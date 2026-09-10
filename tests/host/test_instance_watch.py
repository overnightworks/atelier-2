"""What `atelier2 watch` reports against a served instance's fixed doors.

Two kinds of proof, because the command is two halves in two processes. What
an answer *means* is proven through the `httpx.MockTransport` seam, driving
the very halves the command runs -- `send_reading`, which is what the reading
process reads with, and `absorb_record`, which is what the reporting process
does with every record it receives. A mock transport cannot cross into another
process, so the reading process itself is proven against `_LoopbackServer`, a
real server on loopback that hangs, floods, resets, and lies on purpose, and
against the reading processes in `reader_entries`, each of which goes wrong in
one way a production reader could: that a deadline ends the reading and leaves
no process and no socket behind, that a reader which dies or breaks is never
read as a clean instance, and that nothing the transport libraries say about a
real reply reaches this command's output.

Never the live instance: #1502 forbids that until the operator has ruled on
the observer contract.
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import gzip
import json
import os
import signal
import socket
import ssl
import struct
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from threading import Event, Thread
from typing import Self

import httpx
import pytest

from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import PROBLEM_TYPE_PREFIX, problem_resource
from atelier2.api.seat import SeatState
from atelier2.api.wire.resources import (
    DurableStateCorruptProblemResource,
    HealthResource,
    ProblemResource,
    RedeployBlockedResource,
    RunProjectionCorruptResource,
    SeatResource,
    StreamFailureResource,
)
from atelier2.host import instance_watch
from atelier2.host.atelier_api_client import (
    EVENT_STREAM_MEDIA_TYPE,
    JSON_MEDIA_TYPE,
    PROBLEM_MEDIA_TYPE,
    AtelierApi,
    AtelierApiTransportFailure,
    EventSampleOutcome,
    EventSampleTally,
    TransportFailureCategory,
)
from atelier2.host.instance_reader import (
    EVENTS_PATH,
    FRAME_LIMIT_BYTES,
    HEALTH_PATH,
    MAXIMUM_RESPONSE_BYTES,
    READER_MODULE,
    READER_STOP_GRACE_SECONDS,
    RUN_PATH,
    SEAT_PATH,
    WATCH_ENDPOINTS,
    WORKFLOW_REVISIONS_PATH,
    DoorRead,
    InstanceReading,
    ReaderFailure,
    ReaderPhase,
    ReadingBudget,
    ReadingEnd,
    ReadingRecord,
    absorb_record,
    read_instance,
    send_reading,
    supervised_reading,
)
from atelier2.host.instance_watch import (
    AttentionFeedSample,
    WatchFinding,
    WatchFindingKind,
    WatchReport,
    execute_watch,
    watch_exit_code,
    watch_report,
)
from tests.host.reader_entries import (
    cannot_put_its_client_away,
    dies_before_saying_anything,
    hangs_on_after_closing_the_pipe,
    ignores_being_stopped,
    logs_a_reply_to_a_file,
    names_its_trouble_then_dies,
    signals_its_first_frame,
    stops_mid_record,
    writes_a_length_no_record_has,
)

SERVICE_URL = "http://127.0.0.1:8422"
RECORDED_AT = "2026-09-10T00:00:00Z"
SEAT_TOKEN = "seat-terminal-access-token-9c41"
SEAT_ALIVE_URL = f"http://127.0.0.1:9999/terminal?token={SEAT_TOKEN}"

_TEST_BUDGET = ReadingBudget(
    deadline_seconds=1.0,
    door_read_timeout_seconds=1.0,
    event_sample_read_timeout_seconds=1.0,
)
_TEST_SAMPLE = AttentionFeedSample(
    EventSampleOutcome.SILENT, frames_read=0, bytes_read=0
)
_TEST_READ_TIMEOUT_SECONDS = 1.0
_TEST_MAXIMUM_BYTES = 65_536
_CLEAN_EXIT_CODE = 0

_DEFAULT_HEALTH = HealthResource(
    status="serving",
    source_commit="a" * 40,
    source_tree="b" * 40,
    serve_started_at=RECORDED_AT,
).model_dump_json()
_DEFAULT_SEAT_ALIVE = SeatResource(
    state=SeatState.ALIVE, url=SEAT_ALIVE_URL, project_id="p1"
).model_dump_json()
_DURABLE_STATE_CORRUPT_PROBLEM = DurableStateCorruptProblemResource(
    type="urn:atelier2:problem:v1:durable-state-corrupt",
    title="Durable state is corrupt",
    status=500,
    detail="the attention feed could not project this run",
)


def _sse(*data_payloads: str) -> bytes:
    return "".join(f"data: {payload}\n\n" for payload in data_payloads).encode()


def _feed(content: bytes | Iterator[bytes]) -> httpx.Response:
    """An answer that declares itself the attention feed, which is what the
    sample decides on before it reads one byte of a body."""

    return httpx.Response(
        200, content=content, headers={"content-type": EVENT_STREAM_MEDIA_TYPE}
    )


def _quiet_after(request: httpx.Request, body: bytes) -> httpx.Response:
    """What `/events` answers whenever a test does not pass its own
    `httpx.Response`: the given frames, then quiet -- a real attention feed
    never closes on its own, so every scenario that is not itself testing an
    early close goes silent instead of ending, exactly as production would.
    """

    def content() -> Iterator[bytes]:
        if body:
            yield body
        raise httpx.ReadTimeout("no further attention events", request=request)

    return _feed(content())


def _served(
    overrides: dict[str, bytes | httpx.Response] | None = None,
) -> tuple[httpx.MockTransport, list[str]]:
    """A fixed-endpoint instance: every path answers healthy unless overridden."""

    bodies: dict[str, bytes | httpx.Response] = {
        HEALTH_PATH: _DEFAULT_HEALTH.encode(),
        SEAT_PATH: _DEFAULT_SEAT_ALIVE.encode(),
        RUN_PATH: b"{}",
        WORKFLOW_REVISIONS_PATH: b"{}",
        EVENTS_PATH: b"",
    }
    bodies.update(overrides or {})
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        path = request.url.path.removeprefix(API_PREFIX)
        body = bodies[path]
        if isinstance(body, httpx.Response):
            return body
        if path == EVENTS_PATH:
            return _quiet_after(request, body)
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handle), methods


def _read_here(transport: httpx.BaseTransport) -> InstanceReading:
    """One reading of a served instance, both halves in this process.

    `send_reading` is exactly what the reading process reads with and
    `absorb_record` exactly what the reporting process does with each record
    it receives; the pipe between them is the one thing a mock transport
    cannot cross, and the real server proofs below cover it. The last word
    comes after the client is away, where the reading process sends it.
    """

    reading = InstanceReading()

    def record(sent: ReadingRecord) -> None:
        absorb_record(sent, reading)

    with AtelierApi(SERVICE_URL, transport=transport) as api:
        send_reading(api, _TEST_BUDGET, record)
    record(ReadingEnd())
    reading.reader_exit_code = _CLEAN_EXIT_CODE
    return reading


def _watched(overrides: dict[str, bytes | httpx.Response] | None = None) -> WatchReport:
    transport, _ = _served(overrides)
    return watch_report(SERVICE_URL, _read_here(transport), _TEST_BUDGET)


def test_a_healthy_instance_reports_nothing_and_exits_clean() -> None:
    report = _watched()

    assert report.findings == ()
    assert report.endpoints_read == WATCH_ENDPOINTS
    assert watch_exit_code(report) == 0


def test_the_night_to_10_09_reports_exactly_the_stream_and_seat_finding() -> None:
    report = _watched(
        {
            EVENTS_PATH: _sse(
                StreamFailureResource(
                    problem=problem_resource("durable-state-corrupt")
                ).model_dump_json()
            ),
            SEAT_PATH: SeatResource(state=SeatState.MISSING).model_dump_json().encode(),
        }
    )

    assert {finding.kind for finding in report.findings} == {
        WatchFindingKind.STREAM_FAILED,
        WatchFindingKind.SEAT_NOT_ALIVE,
    }
    assert len(report.findings) == 2
    (seat_finding,) = (
        finding for finding in report.findings if finding.endpoint == SEAT_PATH
    )
    assert seat_finding.detail == "MISSING"
    assert watch_exit_code(report) != 0


def test_a_run_projection_corrupt_frame_names_the_class_and_the_way_not_the_run() -> (
    None
):
    """The reference is a free string an answer can dress up as one, so no
    finding prints it: the class of defect and where a person looks is the
    whole diagnosis."""

    corrupt = RunProjectionCorruptResource(
        public_run_reference=f"run1.{_LEAK_SENTINEL}",
        problem=_DURABLE_STATE_CORRUPT_PROBLEM,
    )
    report = _watched(
        {EVENTS_PATH: _sse('{"event": "AGENT_COMPLETED"}', corrupt.model_dump_json())}
    )

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.RUN_PROJECTION_CORRUPT
    assert "Workbench" in finding.detail
    assert _LEAK_SENTINEL not in json.dumps(dataclasses.asdict(report))


def test_health_redeploy_blocked_is_reported_and_its_absence_is_clean() -> None:
    """`blocked_since` is pattern-locked and safe to name; `.reason` is free
    text the watcher wrote about its own failure and must never appear."""

    blocked = HealthResource(
        status="serving",
        source_commit="a" * 40,
        source_tree="b" * 40,
        serve_started_at=RECORDED_AT,
        redeploy=RedeployBlockedResource(
            blocked_since=RECORDED_AT, reason="SENTINEL-REDEPLOY-REASON"
        ),
    )
    report = _watched({HEALTH_PATH: blocked.model_dump_json().encode()})

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.REDEPLOY_BLOCKED
    assert RECORDED_AT in finding.detail
    assert "SENTINEL-REDEPLOY-REASON" not in json.dumps(dataclasses.asdict(report))


def test_a_non_2xx_listing_answer_is_reported_for_its_own_endpoint() -> None:
    refusal = httpx.Response(
        500, content=problem_resource("internal-error").model_dump_json().encode()
    )
    report = _watched({RUN_PATH: refusal})

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.RESPONSE_REFUSED
    assert finding.endpoint == RUN_PATH


def test_unreachable_service_is_reported_per_attempted_endpoint() -> None:
    def refuse_to_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    report = watch_report(
        SERVICE_URL,
        _read_here(httpx.MockTransport(refuse_to_connect)),
        _TEST_BUDGET,
    )

    assert report.findings
    assert all(
        finding.kind == WatchFindingKind.SERVICE_UNREACHABLE
        for finding in report.findings
    )
    assert {finding.endpoint for finding in report.findings} == set(WATCH_ENDPOINTS)


def test_a_frame_that_is_not_readable_json_is_reported_without_hanging() -> None:
    report = _watched({EVENTS_PATH: _sse("not-json-at-all")})

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.STREAM_SAMPLE_UNREADABLE


def test_every_call_this_command_makes_is_a_get() -> None:
    transport, methods = _served()
    _read_here(transport)

    assert methods
    assert all(method == "GET" for method in methods)
    assert len(methods) == len(WATCH_ENDPOINTS)


def test_the_seat_address_never_reaches_the_report() -> None:
    report = _watched()

    serialized = json.dumps(dataclasses.asdict(report))
    assert SEAT_TOKEN not in serialized
    assert SEAT_ALIVE_URL not in serialized


def test_a_broken_seat_document_never_leaks_its_sentinel_value_through_a_validation_error() -> (
    None
):
    """A seat document missing `state` is invalid -- but pydantic's own
    `ValidationError` embeds the *rest of the input* alongside a `missing`
    error, so a naive `str(error)` would hand the seat's own token back out
    through the very channel meant to keep it in."""

    # Short on purpose: pydantic truncates a long `input_value` with an
    # ellipsis, which would let a careless assertion pass for the wrong
    # reason -- this sentinel is short enough to survive that truncation
    # whole, so finding it absent is real proof, not a truncation accident.
    sentinel = "SEAT-TOKEN-7f3c9"
    broken_seat = json.dumps({"url": f"http://x/?token={sentinel}"}).encode()

    report = _watched({SEAT_PATH: broken_seat})

    assert sentinel not in json.dumps(dataclasses.asdict(report))


def test_a_problem_document_answered_with_200_is_still_reported() -> None:
    body = problem_resource("internal-error").model_dump_json().encode()

    report = _watched({RUN_PATH: httpx.Response(200, content=body)})

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.RESPONSE_REFUSED
    assert finding.endpoint == RUN_PATH


def test_a_bare_problem_document_on_the_event_stream_is_reported() -> None:
    """A problem document that still arrives wrapped in `data:` framing --
    the SSE-classified path, distinct from a truly raw body (below)."""

    body = problem_resource("internal-error").model_dump_json()

    report = _watched({EVENTS_PATH: _feed(_sse(body))})

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.RESPONSE_REFUSED
    assert finding.endpoint == EVENTS_PATH


def test_a_raw_problem_document_on_the_event_stream_is_reported() -> None:
    """A route caught before it ever starts streaming answers its own
    content type and a plain JSON body -- not one `data:` line in sight. Its
    body is the one a refused sample still reads, because it is what says
    which problem this is."""

    body = problem_resource("internal-error").model_dump_json().encode()
    raw = httpx.Response(
        200, content=body, headers={"content-type": PROBLEM_MEDIA_TYPE}
    )

    report = _watched({EVENTS_PATH: raw})

    assert report.attention_feed_sample.outcome is EventSampleOutcome.REFUSED
    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.RESPONSE_REFUSED
    assert finding.endpoint == EVENTS_PATH


def test_an_unknown_problem_type_never_echoes_its_title_or_type() -> None:
    """`title` and `type` are free text a served problem document does not
    have to keep honest -- only a type this repository's own vocabulary
    recognizes ever reaches the report; anything else says only that."""

    sentinel_type = f"{PROBLEM_TYPE_PREFIX}SENTINEL-TYPE-9f2"
    sentinel_title = "SENTINEL-TITLE-4c1"
    body = ProblemResource(
        type=sentinel_type, title=sentinel_title, status=500, detail="whatever"
    ).model_dump_json()

    report = _watched({RUN_PATH: httpx.Response(200, content=body.encode())})

    serialized = json.dumps(dataclasses.asdict(report))
    assert "SENTINEL-TYPE-9f2" not in serialized
    assert sentinel_title not in serialized
    (finding,) = report.findings
    assert finding.detail == "500 unknown problem type"


def test_the_attention_feed_sample_outcome_is_named_in_the_report() -> None:
    content = _sse(*(f'{{"n": {n}}}' for n in range(5)))
    report = _watched({EVENTS_PATH: _feed(content)})

    assert report.attention_feed_sample.outcome is EventSampleOutcome.CLOSED_EARLY
    assert report.attention_feed_sample.frames_read == 5


def test_a_feed_that_closes_before_sending_anything_is_a_finding() -> None:
    report = _watched({EVENTS_PATH: _feed(b"")})

    assert report.attention_feed_sample.outcome is EventSampleOutcome.CLOSED_EARLY
    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.STREAM_NEVER_ANSWERED
    assert finding.endpoint == EVENTS_PATH
    assert "before it ever sent a byte" in finding.detail


@pytest.mark.parametrize(
    ("label", "answer"),
    [
        ("a heartbeat comment, then the connection closes", _feed(b": heartbeat\n\n")),
        (
            "a complete frame that carries no data line",
            _feed(b"id: 42\nevent: ping\n\n"),
        ),
    ],
    ids=["comment-then-close", "no-data-line"],
)
def test_bytes_that_never_complete_a_frame_are_never_a_clean_report(
    label: str, answer: httpx.Response
) -> None:
    """No frame does not mean no bytes. Whatever those bytes were, the feed
    did not answer as a feed within the whole sample, and a report that called
    that clean would be the watcher lying about an instance it could not
    read."""

    del label
    report = _watched({EVENTS_PATH: answer})

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.STREAM_NEVER_ANSWERED
    assert "never completed one data frame" in finding.detail
    assert report.attention_feed_sample.bytes_read > 0
    assert watch_exit_code(report) != 0


def test_a_quiet_feed_that_stays_open_is_not_itself_a_finding() -> None:
    report = _watched()

    assert report.attention_feed_sample.outcome is EventSampleOutcome.SILENT
    assert report.findings == ()


def test_a_trickling_html_answer_on_the_feed_is_reported_rather_than_called_clean() -> (
    None
):
    """The whole report, not only the client's refusal: a 200 that never was
    a stream must leave a finding behind."""

    def trickling_html() -> Iterator[bytes]:
        while True:
            time.sleep(_DRIBBLE_INTERVAL_SECONDS)
            yield b"<p>still here</p>"

    report = _watched(
        {
            EVENTS_PATH: httpx.Response(
                200, content=trickling_html(), headers={"content-type": "text/html"}
            )
        }
    )

    assert report.attention_feed_sample.outcome is EventSampleOutcome.REFUSED
    (finding,) = report.findings
    assert finding.endpoint == EVENTS_PATH
    assert finding.kind == WatchFindingKind.RESPONSE_REFUSED
    assert watch_exit_code(report) != 0


def test_a_reading_the_deadline_cut_short_reports_what_it_had() -> None:
    """The report is built from whatever records arrived: a frame already read
    becomes its finding, and the cut is named beside it."""

    failure_frame = StreamFailureResource(
        problem=problem_resource("durable-state-corrupt")
    ).model_dump_json()
    reading = InstanceReading(deadline_passed=True)
    reading.feed.frames.append(failure_frame)
    reading.feed.bytes_read = len(failure_frame)
    reading.feed.outcome = EventSampleOutcome.INTERRUPTED

    report = watch_report(SERVICE_URL, reading, _TEST_BUDGET)

    assert {finding.kind for finding in report.findings} == {
        WatchFindingKind.STREAM_FAILED,
        WatchFindingKind.READING_CUT_SHORT,
    }
    assert report.attention_feed_sample.frames_read == 1


_LEAK_SENTINEL = "SENTINEL-LEAK-7d3fa1"
_SWEEP_MAXIMUM_BYTES = 65_536


def _poisoned(text: str) -> str:
    return f"{text}{_LEAK_SENTINEL}"


def _poisoned_fixtures() -> tuple[tuple[str, dict[str, bytes | httpx.Response]], ...]:
    """Every kind of answer this command reads, with every free-text field
    and every extension-field key it could carry set to `_LEAK_SENTINEL` --
    the rule itself proven once, in general, rather than one field at a
    time the way each of the last three rounds found a new one.
    """

    poisoned_problem = ProblemResource(
        type=f"{PROBLEM_TYPE_PREFIX}{_poisoned('unknown-code')}",
        title=_poisoned("Some Title"),
        status=500,
        detail=_poisoned("Some detail"),
    )
    poisoned_stream_failure = StreamFailureResource(problem=poisoned_problem)
    poisoned_corrupt = RunProjectionCorruptResource(
        public_run_reference="run1.aGVhbHRoeQ",
        problem=DurableStateCorruptProblemResource(
            type="urn:atelier2:problem:v1:durable-state-corrupt",
            title="Durable state is corrupt",
            status=500,
            detail=_poisoned("some detail"),
        ),
    )
    return (
        (
            "health fields this command never reads at all",
            {
                HEALTH_PATH: HealthResource(
                    status="serving",
                    source_commit=_poisoned("a" * 40),
                    source_tree=_poisoned("b" * 40),
                    serve_started_at=RECORDED_AT,
                )
                .model_dump_json()
                .encode()
            },
        ),
        (
            "a redeploy block's reason",
            {
                HEALTH_PATH: HealthResource(
                    status="serving",
                    source_commit="a" * 40,
                    source_tree="b" * 40,
                    serve_started_at=RECORDED_AT,
                    redeploy=RedeployBlockedResource(
                        blocked_since=RECORDED_AT, reason=_poisoned("stuck")
                    ),
                )
                .model_dump_json()
                .encode()
            },
        ),
        (
            "a living seat's url and project id",
            {
                SEAT_PATH: SeatResource(
                    state=SeatState.ALIVE,
                    url=_poisoned("http://127.0.0.1:9/terminal"),
                    project_id=_poisoned("p1"),
                )
                .model_dump_json()
                .encode()
            },
        ),
        (
            "a broken seat document's sibling value and its extra field's own key",
            {
                SEAT_PATH: json.dumps(
                    {
                        "url": _poisoned("http://127.0.0.1:9/terminal"),
                        _poisoned("extra-field-name"): "value",
                    }
                ).encode()
            },
        ),
        (
            "a problem document answered with 200",
            {
                RUN_PATH: httpx.Response(
                    200, content=poisoned_problem.model_dump_json().encode()
                )
            },
        ),
        (
            "a non-2xx answer's reason phrase and its problem body",
            {
                RUN_PATH: httpx.Response(
                    500,
                    content=poisoned_problem.model_dump_json().encode(),
                    extensions={
                        "reason_phrase": _poisoned("Internal Server Error").encode()
                    },
                )
            },
        ),
        (
            "a refused content-encoding",
            {
                HEALTH_PATH: httpx.Response(
                    200, content=b"{}", headers={"content-encoding": _poisoned("gzip")}
                )
            },
        ),
        (
            "a refused content-type on the event stream",
            {
                EVENTS_PATH: httpx.Response(
                    200,
                    content=poisoned_problem.model_dump_json().encode(),
                    headers={"content-type": _poisoned("application/problem+json")},
                )
            },
        ),
        (
            "a refused problem document on the event stream",
            {
                EVENTS_PATH: httpx.Response(
                    200,
                    content=poisoned_problem.model_dump_json().encode(),
                    headers={"content-type": PROBLEM_MEDIA_TYPE},
                )
            },
        ),
        (
            "a STREAM_FAILED frame's problem",
            {EVENTS_PATH: _feed(_sse(poisoned_stream_failure.model_dump_json()))},
        ),
        (
            "a RUN_PROJECTION_CORRUPT frame's problem detail",
            {EVENTS_PATH: _feed(_sse(poisoned_corrupt.model_dump_json()))},
        ),
        (
            "a RUN_PROJECTION_CORRUPT frame's own run reference",
            {
                EVENTS_PATH: _feed(
                    _sse(
                        RunProjectionCorruptResource(
                            public_run_reference=f"run1.{_LEAK_SENTINEL}",
                            problem=_DURABLE_STATE_CORRUPT_PROBLEM,
                        ).model_dump_json()
                    )
                )
            },
        ),
        (
            "bytes on the event stream that are not UTF-8 text",
            {EVENTS_PATH: _feed(b"data: \xff\xfe" + _LEAK_SENTINEL.encode() + b"\n\n")},
        ),
    )


_POISONED_FIXTURES = _poisoned_fixtures()


@pytest.mark.parametrize(
    ("label", "overrides"),
    _POISONED_FIXTURES,
    ids=[label for label, _ in _POISONED_FIXTURES],
)
def test_no_sentinel_from_any_answer_ever_reaches_the_report(
    label: str, overrides: dict[str, bytes | httpx.Response]
) -> None:
    """The report this command prints, for every answer shape it reads: not
    one character an answer wrote may appear in it.

    The other channel a character could leave through -- what the transport
    libraries log about a reply -- belongs to the reading process, and is
    proven where that process is real, against a talkative server below.
    """

    del label
    transport, _ = _served(overrides)
    report = watch_report(SERVICE_URL, _read_here(transport), _TEST_BUDGET)

    assert _LEAK_SENTINEL not in json.dumps(dataclasses.asdict(report))


@pytest.mark.parametrize(
    ("label", "overrides"),
    _POISONED_FIXTURES,
    ids=[label for label, _ in _POISONED_FIXTURES],
)
def test_no_sentinel_survives_in_a_failure_the_watcher_itself_consumes(
    label: str, overrides: dict[str, bytes | httpx.Response]
) -> None:
    """The report above only shows what was classified; the reading swallows
    most failures into records. This reads the same answers at the client
    boundary the reading reads them through and inspects the failure it hands
    over: its message, its reason, and its whole `__cause__` chain -- which
    must be empty, because a chained library exception carries the far side's
    own text into every traceback that ever prints it.

    `failure.body` is deliberately not swept: those are the answer's own
    bytes, handed over so the reporting side can recognize a problem document
    in them, and no channel ever prints them.
    """

    del label
    transport, _ = _served(overrides)
    with AtelierApi(SERVICE_URL, transport=transport) as api:
        for endpoint in (HEALTH_PATH, SEAT_PATH, RUN_PATH, WORKFLOW_REVISIONS_PATH):
            try:
                api.bounded_get(
                    endpoint,
                    read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
                    maximum_bytes=_SWEEP_MAXIMUM_BYTES,
                )
            except AtelierApiTransportFailure as failure:
                _assert_carries_no_sentinel(failure)
        try:
            api.sampled_event_frames(
                EVENTS_PATH,
                accept=EVENT_STREAM_MEDIA_TYPE,
                read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
                maximum_bytes=_SWEEP_MAXIMUM_BYTES,
                maximum_frames=10,
                on_frame=lambda data: None,
                tally=EventSampleTally(),
            )
        except AtelierApiTransportFailure as failure:
            _assert_carries_no_sentinel(failure)


def _assert_carries_no_sentinel(failure: AtelierApiTransportFailure) -> None:
    assert failure.__cause__ is None
    assert _LEAK_SENTINEL not in f"{failure}{failure.reason}"


def _caused(error: Exception, cause: BaseException | None) -> Exception:
    error.__cause__ = cause
    return error


def _wrapped_the_way_httpcore_does(
    error: Exception, inner: Exception, underneath: BaseException
) -> Exception:
    """The chain a real connection failure arrives in: httpx chains its own
    wrapper explicitly, while httpcore re-raises inside the original's handler
    and leaves only `__context__` behind."""

    inner.__context__ = underneath
    return _caused(error, inner)


_FAILURE_CATEGORIES: tuple[tuple[str, Exception, str], ...] = (
    (
        "a name that does not resolve",
        _wrapped_the_way_httpcore_does(
            httpx.ConnectError(_poisoned("gai: ")),
            httpx.ConnectError(_poisoned("httpcore gai: ")),
            socket.gaierror(-2, _poisoned("Name or service not known ")),
        ),
        "dns",
    ),
    (
        "a certificate that does not verify",
        _caused(
            httpx.ConnectError(_poisoned("ssl: ")),
            ssl.SSLCertVerificationError(_poisoned("certificate verify failed ")),
        ),
        "tls",
    ),
    (
        "a port that says no",
        _wrapped_the_way_httpcore_does(
            httpx.ConnectError(_poisoned("refused: ")),
            httpx.ConnectError(_poisoned("httpcore refused: ")),
            ConnectionRefusedError(111, _poisoned("Connection refused ")),
        ),
        "refused",
    ),
    (
        "a peer that hangs up mid-read",
        _caused(
            httpx.ReadError(_poisoned("reset: ")),
            ConnectionResetError(104, _poisoned("Connection reset by peer ")),
        ),
        "reset",
    ),
    ("a connection that never comes up", httpx.ConnectTimeout("timed out"), "timeout"),
    (
        "a reply that breaks the protocol",
        httpx.RemoteProtocolError(_poisoned("server disconnected ")),
        "protocol",
    ),
    (
        "a connection failure that says nothing more",
        httpx.ConnectError(_poisoned("proxy said: ")),
        "unclassified",
    ),
)


@pytest.mark.parametrize(
    ("label", "error", "category"),
    _FAILURE_CATEGORIES,
    ids=[label for label, _, _ in _FAILURE_CATEGORIES],
)
def test_a_transport_failure_is_named_by_its_category_and_nothing_else(
    label: str, error: Exception, category: str
) -> None:
    """A library's class alone loses what matters -- one `ConnectError` covers
    a name that does not resolve, a certificate that does not verify, and a
    port that says no -- while its message can carry the far side's own text.
    The category is this module's own word, and nothing is chained."""

    del label

    def raise_it(request: httpx.Request) -> httpx.Response:
        del request
        raise error

    with (
        AtelierApi(SERVICE_URL, transport=httpx.MockTransport(raise_it)) as api,
        pytest.raises(AtelierApiTransportFailure) as raised,
    ):
        api.bounded_get(
            HEALTH_PATH,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=1_000,
        )

    assert raised.value.reason == f"transport failure: {category}"
    assert raised.value.__cause__ is None
    assert _LEAK_SENTINEL not in raised.value.reason


def test_bounded_get_never_exceeds_its_byte_cap_without_hanging() -> None:
    content = b"x" * 10_000

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content)

    with (
        AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api,
        pytest.raises(AtelierApiTransportFailure),
    ):
        api.bounded_get(
            HEALTH_PATH,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=100,
        )


def test_bounded_reads_ask_for_identity_encoding() -> None:
    seen: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        if request.url.path.endswith(EVENTS_PATH):
            return _feed(b"")
        return httpx.Response(200, content=b"{}")

    with AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api:
        api.bounded_get(
            HEALTH_PATH,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=1_000,
        )
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=1_000,
            maximum_frames=10,
            on_frame=lambda data: None,
            tally=EventSampleTally(),
        )

    assert seen == ["identity", "identity"]


def test_a_compressed_reply_is_refused_before_its_body_is_ever_read() -> None:
    """`Accept-Encoding: identity` is only a request; a reply that answers
    compressed anyway must be refused by its header alone, before this call
    ever asks `iter_bytes` to decode a single byte of it -- otherwise the
    decoded result could already have outgrown the byte cap before the cap
    ever saw it."""

    real_gzip = gzip.compress(b"far more bytes once decompressed than compressed")

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, content=real_gzip, headers={"content-encoding": "gzip"}
        )

    with (
        AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api,
        pytest.raises(AtelierApiTransportFailure) as failure,
    ):
        api.bounded_get(
            HEALTH_PATH,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=4,
        )

    assert "content-encoding" in failure.value.reason
    assert failure.value.body == b""


def test_a_compressed_event_stream_reply_is_refused_before_being_read() -> None:
    real_gzip = gzip.compress(b"data: hello\n\n")

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=real_gzip,
            headers={
                "content-encoding": "gzip",
                "content-type": EVENT_STREAM_MEDIA_TYPE,
            },
        )

    with (
        AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api,
        pytest.raises(AtelierApiTransportFailure) as failure,
    ):
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=1_000,
            maximum_frames=10,
            on_frame=lambda data: None,
            tally=EventSampleTally(),
        )

    assert "content-encoding" in failure.value.reason


def test_a_content_type_that_is_not_a_stream_is_refused_before_one_body_byte() -> None:
    """The decision falls on the header. An error page whose body trickles
    under every read timeout would otherwise run out the whole reading and
    then look like a stream that merely sent no frame."""

    opened: list[bool] = []

    def never_ending_html() -> Iterator[bytes]:
        opened.append(True)
        while True:
            yield b"<p>still here</p>"

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, content=never_ending_html(), headers={"content-type": "text/html"}
        )

    with (
        AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api,
        pytest.raises(AtelierApiTransportFailure) as failure,
    ):
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=_TEST_MAXIMUM_BYTES,
            maximum_frames=10,
            on_frame=lambda data: None,
            tally=EventSampleTally(),
        )

    assert opened == []
    assert failure.value.body == b""
    assert "not an event stream" in failure.value.reason


def test_a_bounded_get_that_hits_its_byte_cap_still_closes_the_response() -> None:
    """`bounded_get` aborting early -- here, over its own byte cap -- must
    still leave httpx's own close protocol run: `is_closed` is what httpx
    itself sets once a response's connection is released, regardless of
    whether the mock content behind it (a plain Python generator, unlike a
    real socket) has a `close()` of its own to call."""

    def never_ending_chunks() -> Iterator[bytes]:
        while True:
            yield b"x" * 100

    responses: list[httpx.Response] = []

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        response = httpx.Response(200, content=never_ending_chunks())
        responses.append(response)
        return response

    with (
        AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api,
        pytest.raises(AtelierApiTransportFailure),
    ):
        api.bounded_get(
            HEALTH_PATH,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=250,
        )

    (response,) = responses
    assert response.is_closed


def test_a_silent_event_feed_stops_the_sample_instead_of_hanging() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        def body() -> Iterator[bytes]:
            yield b": lead-in\n\n"
            raise httpx.ReadTimeout("no data arrived", request=request)

        return _feed(body())

    frames: list[str] = []
    tally = EventSampleTally()
    with AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api:
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=0.01,
            maximum_bytes=1_000,
            maximum_frames=5,
            on_frame=frames.append,
            tally=tally,
        )

    assert tally.outcome is EventSampleOutcome.SILENT
    assert frames == []


def test_a_stream_that_closes_before_any_budget_is_hit_is_named_closed_early() -> None:
    frames: list[str] = []
    tally = EventSampleTally()
    with AtelierApi(
        SERVICE_URL,
        transport=httpx.MockTransport(
            lambda request: _feed(_sse('{"event": "AGENT_COMPLETED"}'))
        ),
    ) as api:
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=1_000_000,
            maximum_frames=100,
            on_frame=frames.append,
            tally=tally,
        )

    assert frames == ['{"event": "AGENT_COMPLETED"}']
    assert tally.outcome is EventSampleOutcome.CLOSED_EARLY


def test_reading_stops_at_the_frame_limit_without_hanging() -> None:
    content = _sse(*(f'{{"n": {n}}}' for n in range(5)))
    frames: list[str] = []
    tally = EventSampleTally()
    with AtelierApi(
        SERVICE_URL, transport=httpx.MockTransport(lambda request: _feed(content))
    ) as api:
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=1_000_000,
            maximum_frames=2,
            on_frame=frames.append,
            tally=tally,
        )

    assert len(frames) == 2
    assert tally.outcome is EventSampleOutcome.FRAME_LIMIT


def test_reading_stops_at_the_byte_limit_without_hanging() -> None:
    chunks = (b"data: a\n\n", b"data: b\n\n", b"data: c\n\n")
    frames: list[str] = []
    tally = EventSampleTally()
    with AtelierApi(
        SERVICE_URL, transport=httpx.MockTransport(lambda request: _feed(iter(chunks)))
    ) as api:
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=len(chunks[0]),
            maximum_frames=100,
            on_frame=frames.append,
            tally=tally,
        )

    assert frames == ["a"]
    assert tally.outcome is EventSampleOutcome.BYTE_LIMIT


def test_execute_watch_prints_one_json_report_whose_exit_code_matches_its_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    finding = WatchFinding(WatchFindingKind.SEAT_NOT_ALIVE, SEAT_PATH, "MISSING")
    canned = WatchReport(
        service_url=SERVICE_URL,
        endpoints_read=WATCH_ENDPOINTS,
        findings=(finding,),
        attention_feed_sample=AttentionFeedSample(
            EventSampleOutcome.INTERRUPTED, frames_read=2, bytes_read=412
        ),
        budget=_TEST_BUDGET,
    )
    monkeypatch.setattr(
        instance_watch, "watch_instance", lambda service_url, budget: canned
    )

    exit_code = execute_watch(argparse.Namespace(service=SERVICE_URL))

    printed = json.loads(capsys.readouterr().out)
    assert printed["service_url"] == SERVICE_URL
    assert printed["attention_feed_sample"] == {
        "outcome": "interrupted",
        "frames_read": 2,
        "bytes_read": 412,
    }
    assert printed["budget"] == {
        "deadline_seconds": 1.0,
        "door_read_timeout_seconds": 1.0,
        "event_sample_read_timeout_seconds": 1.0,
    }
    assert printed["findings"] == [
        {"kind": "SEAT_NOT_ALIVE", "endpoint": SEAT_PATH, "detail": "MISSING"}
    ]
    assert exit_code != 0


def test_watch_exit_code_is_zero_only_without_findings() -> None:
    clean = WatchReport(
        service_url=SERVICE_URL,
        endpoints_read=WATCH_ENDPOINTS,
        findings=(),
        attention_feed_sample=_TEST_SAMPLE,
        budget=_TEST_BUDGET,
    )
    dirty = WatchReport(
        service_url=SERVICE_URL,
        endpoints_read=WATCH_ENDPOINTS,
        findings=(WatchFinding(WatchFindingKind.SEAT_NOT_ALIVE, SEAT_PATH, "MISSING"),),
        attention_feed_sample=_TEST_SAMPLE,
        budget=_TEST_BUDGET,
    )

    assert watch_exit_code(clean) == 0
    assert watch_exit_code(dirty) != 0


_DRIBBLE_INTERVAL_SECONDS = 0.02
_ACCEPT_POLL_SECONDS = 0.05
_SERVER_JOIN_SECONDS = 5.0

_HEAD_END = b"\r\n\r\n"

_BODY_THAT_FILLS_THE_CAP = b'{"filler":"' + b"x" * (MAXIMUM_RESPONSE_BYTES - 20) + b'"}'
"""As much as one bounded read may keep, and more than one read of the pipe
between the two processes carries."""
_DEADLINE_TOLERANCE_SECONDS = 10.0
"""How much later than its deadline a reading may return and still count as
bounded -- generous, because what is proven is that it returns at all, not how
promptly a shared machine schedules a fresh interpreter."""

_REAL_READ_BUDGET = ReadingBudget(
    deadline_seconds=25.0,
    door_read_timeout_seconds=0.5,
    event_sample_read_timeout_seconds=2.0,
)
"""For a real reading whose own read timeouts are what end it: the deadline is
wide, because the reading process's start is inside it, and each read timeout
is long enough that a server which answers at loopback speed is never read as
having gone quiet."""

_HEALTHY_READ_BUDGET = dataclasses.replace(
    _REAL_READ_BUDGET, event_sample_read_timeout_seconds=0.5
)
"""For the one reading that must come back with nothing to report: a feed that
stays open and silent ends the sample after this, and a whole healthy instance
is read in about that."""

_REAL_DEADLINE_BUDGET = ReadingBudget(
    deadline_seconds=5.0,
    door_read_timeout_seconds=30.0,
    event_sample_read_timeout_seconds=30.0,
)
"""For a real reading only the deadline can end: every read timeout is far
past it, and the deadline itself is far past the reading process's start."""

_STUBBORN_READER_BUDGET = ReadingBudget(
    deadline_seconds=4.0,
    door_read_timeout_seconds=30.0,
    event_sample_read_timeout_seconds=30.0,
)
"""Long enough that the reading process is certainly up and has taken the
signal it means to ignore before the deadline reaches it."""

_STOP_BUDGET_SECONDS = 3 * READER_STOP_GRACE_SECONDS
"""The whole stopping: one wait for a clean exit, one after a stop, one after
a kill."""

_UNUSABLE_ADDRESS = "nowhere-a-client-could-reach"

_CHATTER_REASON_PHRASE = "SENTINEL-REASON-PHRASE"
_CHATTER_HEADER_VALUE = "SENTINEL-HEADER-VALUE"

_STREAM_FAILED_FRAME = StreamFailureResource(
    problem=problem_resource("durable-state-corrupt")
).model_dump_json()


class _LoopbackServer:
    """A real TCP server on loopback that hangs, floods, resets, and lies.

    `httpx.MockTransport` cannot stand in for this, and cannot even reach the
    reading: it lives in another process, so every proof about that process --
    that a deadline leaves no socket and no child behind, what its transport
    libraries print -- needs a real address. `speak` is what this server does
    with an accepted connection; afterwards it reads until the client closes,
    which is what `connections_closed_by_client` counts -- a peer's own FIN,
    the socket-level proof that the reading cleaned up after itself.
    """

    def __init__(self, speak: Callable[[socket.socket, Event], None]) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._speak = speak
        self._stop = Event()
        self._thread = Thread(target=self._serve, name="loopback-server")
        self.connections_accepted = 0
        self.connections_closed_by_client = 0

    @property
    def url(self) -> str:
        host, port = self._listener.getsockname()[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exception: object) -> None:
        self._stop.set()
        self._thread.join(_SERVER_JOIN_SECONDS)
        self._listener.close()
        assert not self._thread.is_alive()

    def _serve(self) -> None:
        self._listener.settimeout(_ACCEPT_POLL_SECONDS)
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections_accepted += 1
            with connection:
                self._speak(connection, self._stop)
                if _read_until_the_client_closes(connection):
                    self.connections_closed_by_client += 1


def _read_until_the_client_closes(connection: socket.socket) -> bool:
    """Whether this peer said goodbye rather than vanishing -- which a speaker
    that tore its own connection down never did."""

    try:
        connection.settimeout(_SERVER_JOIN_SECONDS)
        while connection.recv(4_096):
            pass
    except OSError:
        return False
    return True


def _waited_for(condition: Callable[[], bool]) -> bool:
    """Whether `condition` came true within the server's own join budget.

    The server counts a peer's FIN on its own thread, so a socket-level fact
    is observable a moment after the call that caused it returned."""

    until = time.monotonic() + _SERVER_JOIN_SECONDS
    while time.monotonic() < until and not condition():
        time.sleep(_ACCEPT_POLL_SECONDS)
    return condition()


def _stay_silent(connection: socket.socket, stop: Event) -> None:
    """Accept the request and answer nothing at all -- not one header byte."""

    del connection, stop


def _sent(connection: socket.socket, payload: bytes) -> None:
    try:
        connection.sendall(payload)
    except OSError:
        return


def _stream_head() -> bytes:
    """An answer that declares itself an attention feed and ends at EOF, so
    one connection carries one request and this one-at-a-time server answers
    every door."""

    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Connection: close\r\n\r\n"
    )


def _frame_then_bytes_that_are_not_utf8(connection: socket.socket, stop: Event) -> None:
    del stop
    _sent(connection, _stream_head() + _sse(_STREAM_FAILED_FRAME))
    _sent(connection, b"data: \xff\xfe\n\n")


def _frame_then_a_reset_once_it_was_read(
    arrived: Path,
) -> Callable[[socket.socket, Event], None]:
    """A frame, then the peer vanishes: `SO_LINGER` with a zero timeout makes
    the close a reset rather than a graceful goodbye.

    A reset discards whatever the reader had not taken off the wire yet, so
    the frame has to be read before this fires -- and the reader says so
    itself by leaving word at `arrived` (`signals_its_first_frame`), rather
    than this server pausing long enough to hope for it.
    """

    def speak(connection: socket.socket, stop: Event) -> None:
        del stop
        path = _request_path(connection)
        if not path.endswith(EVENTS_PATH):
            _answer_the_door(connection, path)
            return
        _sent(connection, _stream_head() + _sse(_STREAM_FAILED_FRAME))
        _waited_for(arrived.exists)
        connection.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        connection.close()

    return speak


def _request_path(connection: socket.socket) -> str:
    """What this connection asked for, so one speaker can answer the doors and
    the feed differently."""

    head = b""
    try:
        connection.settimeout(_SERVER_JOIN_SECONDS)
        while _HEAD_END not in head:
            arrived = connection.recv(4_096)
            if not arrived:
                break
            head += arrived
    except OSError:
        return ""
    target = head.split(b"\r\n", 1)[0].split(b" ")
    return target[1].decode() if len(target) > 1 else ""


def _json_answer(body: bytes) -> bytes:
    return (
        f"HTTP/1.1 200 OK\r\nContent-Type: {JSON_MEDIA_TYPE}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


def _half_a_frame_after_a_whole_one(connection: socket.socket, stop: Event) -> None:
    """The doors answer at once; the feed sends one whole frame and then half
    of the next, and stops there -- so only the deadline can end the reading,
    and it ends it while a frame is incomplete."""

    if not _request_path(connection).endswith(EVENTS_PATH):
        _sent(connection, _json_answer(b"{}"))
        return
    _sent(connection, _stream_head() + _sse(_STREAM_FAILED_FRAME) + b"data: {half")
    stop.wait(_SERVER_JOIN_SECONDS)


_HEALTHY_DOORS: dict[str, bytes] = {
    HEALTH_PATH: _DEFAULT_HEALTH.encode(),
    SEAT_PATH: _DEFAULT_SEAT_ALIVE.encode(),
    RUN_PATH: b"{}",
    WORKFLOW_REVISIONS_PATH: b"{}",
}
_SILENT_FEED_SECONDS = 2.0
"""Longer than the sample's own read timeout in `_HEALTHY_READ_BUDGET`, so the
feed's silence is what ends the sample rather than this server giving up."""


def _answer_the_door(connection: socket.socket, path: str) -> None:
    """Whichever fixed door this path is, answered with its own published
    document."""

    for door, body in _HEALTHY_DOORS.items():
        if path.endswith(door):
            _sent(connection, _json_answer(body))
            return


def _answer_every_door_healthily(connection: socket.socket, stop: Event) -> None:
    """A whole healthy instance: each door its own published document, and a
    feed that opens and then says nothing.

    The one served shape whose report is empty, so any finding against it can
    only be the reading's own doing.
    """

    path = _request_path(connection)
    if path.endswith(EVENTS_PATH):
        _sent(connection, _stream_head())
        stop.wait(_SILENT_FEED_SECONDS)
        return
    _answer_the_door(connection, path)


def _answer_a_body_that_fills_the_cap(connection: socket.socket, stop: Event) -> None:
    """Every door answers as much as a bounded read may keep, which is more
    than one read of the pipe carries and more than the pipe itself holds --
    so each record has to be written and reassembled in pieces."""

    del stop
    _sent(connection, _json_answer(_BODY_THAT_FILLS_THE_CAP))


def _frame_then_more_bytes_than_the_cap(connection: socket.socket, stop: Event) -> None:
    del stop
    _sent(connection, _stream_head() + _sse(_STREAM_FAILED_FRAME))
    _sent(connection, b"data: " + b"x" * (_TEST_MAXIMUM_BYTES + 1) + b"\n\n")


def _answer_a_problem_document(connection: socket.socket, stop: Event) -> None:
    del stop
    body = problem_resource("internal-error").model_dump_json().encode()
    _sent(
        connection,
        (
            f"HTTP/1.1 500 Internal Server Error\r\n"
            f"Content-Type: {PROBLEM_MEDIA_TYPE}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        + body,
    )


def _answer_with_a_talkative_status_line(
    connection: socket.socket, stop: Event
) -> None:
    """A complete reply whose status line and headers carry text of the far
    side's own choosing -- exactly what httpx logs at `INFO` and httpcore at
    `DEBUG`. `Connection: close` keeps one request to one connection, so this
    one-at-a-time server answers each of them."""

    del stop
    body = b"{}"
    _sent(
        connection,
        (
            f"HTTP/1.1 200 {_CHATTER_REASON_PHRASE}\r\n"
            f"X-Sentinel: {_CHATTER_HEADER_VALUE}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        + body,
    )


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reader_entries_are_on_the_readers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reading processes below are modules of this suite, and a fresh
    interpreter finds them only if the repository root is on its path --
    whatever directory this suite was started from."""

    inherited = os.environ.get("PYTHONPATH")
    path = [str(_REPOSITORY_ROOT), *([inherited] if inherited else [])]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(path))


def test_a_deadline_ends_the_reading_and_leaves_no_process_and_no_socket_behind() -> (
    None
):
    """A real server that accepts and then says nothing, with every read
    timeout set far past the deadline, so only the deadline can end this. It
    ends the reading process with it: the server sees that peer's own close,
    the process was reaped, and the report says where the reading stood
    instead of calling the instance clean."""

    with _LoopbackServer(_stay_silent) as server:
        served_url = server.url
        started = time.monotonic()
        reading = read_instance(served_url, _REAL_DEADLINE_BUDGET)
        elapsed = time.monotonic() - started
        assert _waited_for(lambda: server.connections_accepted == 1)
        assert _waited_for(lambda: server.connections_closed_by_client == 1)

    report = watch_report(served_url, reading, _REAL_DEADLINE_BUDGET)
    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.READING_CUT_SHORT
    assert finding.endpoint == HEALTH_PATH
    assert report.endpoints_read == ()
    assert watch_exit_code(report) != 0
    assert reading.deadline_passed
    assert reading.reader_reaped
    assert reading.reader_exit_code is not None
    assert (
        elapsed < _REAL_DEADLINE_BUDGET.deadline_seconds + _DEADLINE_TOLERANCE_SECONDS
    )


def test_a_deadline_that_has_already_passed_asks_the_instance_nothing() -> None:
    passed = dataclasses.replace(_REAL_DEADLINE_BUDGET, deadline_seconds=0.0)

    with _LoopbackServer(_stay_silent) as server:
        reading = read_instance(server.url, passed)

        assert server.connections_accepted == 0

    assert reading.deadline_passed
    assert reading.doors == []


def test_a_reading_process_that_dies_is_reported_rather_than_read_as_clean() -> None:
    """Silence from a dead reader must never pass for an instance with nothing
    to say: a process that ended without its last word is a finding of its
    own, named with the code it died with."""

    reading = supervised_reading(
        dies_before_saying_anything.__name__, SERVICE_URL, _REAL_READ_BUDGET
    )

    assert reading.reader_exit_code == dies_before_saying_anything.DEATH_CODE
    assert not reading.reader_ended
    assert not reading.deadline_passed
    report = watch_report(SERVICE_URL, reading, _REAL_READ_BUDGET)
    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.READER_DIED
    assert str(dies_before_saying_anything.DEATH_CODE) in finding.detail
    assert watch_exit_code(report) != 0


_REAL_ENDINGS: tuple[
    tuple[
        str,
        Callable[[socket.socket, Event], None],
        set[WatchFindingKind],
        EventSampleOutcome,
        int,
    ],
    ...,
] = (
    (
        "a server that never answers a byte",
        _stay_silent,
        {WatchFindingKind.SERVICE_UNREACHABLE},
        EventSampleOutcome.UNREACHABLE,
        0,
    ),
    (
        "a frame, then bytes that are not UTF-8 text",
        _frame_then_bytes_that_are_not_utf8,
        {WatchFindingKind.STREAM_FAILED, WatchFindingKind.SERVICE_UNREACHABLE},
        EventSampleOutcome.REFUSED,
        1,
    ),
    (
        "a frame, then more bytes than the sample's cap",
        _frame_then_more_bytes_than_the_cap,
        {WatchFindingKind.STREAM_FAILED},
        EventSampleOutcome.BYTE_LIMIT,
        1,
    ),
    (
        "a problem document instead of a stream",
        _answer_a_problem_document,
        {WatchFindingKind.RESPONSE_REFUSED},
        EventSampleOutcome.REFUSED,
        0,
    ),
)


@pytest.mark.parametrize(
    ("label", "speak", "kinds", "outcome", "frames_read"),
    _REAL_ENDINGS,
    ids=[label for label, *_ in _REAL_ENDINGS],
)
def test_every_way_a_real_feed_can_end_reaches_the_report(
    label: str,
    speak: Callable[[socket.socket, Event], None],
    kinds: set[WatchFindingKind],
    outcome: EventSampleOutcome,
    frames_read: int,
) -> None:
    """Each ending path, driven through the reading process against a real
    server: what the sample had read before it ended is reported together with
    how it ended, and no ending is ever silently clean.
    """

    del label
    with _LoopbackServer(speak) as server:
        served_url = server.url
        reading = read_instance(served_url, _REAL_READ_BUDGET)

    report = watch_report(served_url, reading, _REAL_READ_BUDGET)
    assert not reading.deadline_passed
    assert reading.reader_ended
    assert report.attention_feed_sample.outcome is outcome
    assert report.attention_feed_sample.frames_read == frames_read
    assert {
        finding.kind for finding in report.findings if finding.endpoint == EVENTS_PATH
    } == kinds


def test_a_frame_read_before_a_reset_reaches_the_report_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feed that sends a frame and is then torn down under the reader: what
    the sample had already read stands in the report next to how it ended.

    The reading says when it has the frame, so the reset lands at that moment
    rather than after a wait long enough to hope for it.
    """

    arrived = tmp_path / "first-frame"
    monkeypatch.setenv(signals_its_first_frame.FIRST_FRAME_CHANNEL, str(arrived))

    with _LoopbackServer(_frame_then_a_reset_once_it_was_read(arrived)) as server:
        served_url = server.url
        reading = supervised_reading(
            signals_its_first_frame.__name__, served_url, _REAL_READ_BUDGET
        )

    report = watch_report(served_url, reading, _REAL_READ_BUDGET)
    assert reading.reader_ended
    assert report.attention_feed_sample.outcome is EventSampleOutcome.REFUSED
    assert report.attention_feed_sample.frames_read == 1
    assert {finding.kind for finding in report.findings} == {
        WatchFindingKind.STREAM_FAILED,
        WatchFindingKind.SERVICE_UNREACHABLE,
    }


def test_a_reader_that_breaks_names_the_phase_and_leaves_cleanly() -> None:
    """Trouble the reading did not expect is a record, not a traceback nobody
    reads: the phase it happened in and a category from this repository's own
    table, and then a clean exit -- a reader that broke is not a reader that
    died."""

    reading = supervised_reading(READER_MODULE, _UNUSABLE_ADDRESS, _REAL_READ_BUDGET)

    assert reading.failure == ReaderFailure(
        ReaderPhase.SETUP, TransportFailureCategory.UNCLASSIFIED
    )
    assert reading.reader_exit_code == _CLEAN_EXIT_CODE
    assert not reading.reader_ended
    report = watch_report(_UNUSABLE_ADDRESS, reading, _REAL_READ_BUDGET)
    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.READER_FAILED
    assert ReaderPhase.SETUP.value in finding.detail
    assert watch_exit_code(report) != 0


def test_a_reader_that_cannot_put_its_client_away_says_so_and_nothing_else() -> None:
    """A healthy instance read to the end, and then a client that will not be
    put away: the finding names the phase after every door was already read,
    and a cleanup that failed leaves no report that reads as complete -- but a
    reader that broke and then left cleanly is not a reader that died."""

    with _LoopbackServer(_answer_every_door_healthily) as server:
        served_url = server.url
        reading = supervised_reading(
            cannot_put_its_client_away.__name__, served_url, _HEALTHY_READ_BUDGET
        )

    report = watch_report(served_url, reading, _HEALTHY_READ_BUDGET)
    assert len(reading.doors) == len(WATCH_ENDPOINTS) - 1
    assert not reading.reader_ended
    assert reading.reader_exit_code == _CLEAN_EXIT_CODE
    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.READER_FAILED
    assert ReaderPhase.CLEANUP.value in finding.detail


def test_a_reader_that_names_its_trouble_and_dies_anyway_is_both_findings() -> None:
    """A reader that broke and then left cleanly is one finding; one that
    broke and died is two, because the code it ended with is not what it
    reported and nothing in the report may pass that off as accounted for."""

    reading = supervised_reading(
        names_its_trouble_then_dies.__name__, SERVICE_URL, _REAL_READ_BUDGET
    )

    assert reading.failure == names_its_trouble_then_dies.TROUBLE
    assert reading.reader_exit_code == names_its_trouble_then_dies.DEATH_CODE
    report = watch_report(SERVICE_URL, reading, _REAL_READ_BUDGET)
    assert {finding.kind for finding in report.findings} == {
        WatchFindingKind.READER_FAILED,
        WatchFindingKind.READER_DIED,
    }


def test_a_reading_that_stops_mid_record_keeps_what_arrived_whole() -> None:
    """Half a record is never a record: the one that arrived whole before it
    stands in the report, and the reading that stopped in the middle of the
    next is named as one that died rather than one that finished."""

    reading = supervised_reading(
        stops_mid_record.__name__, SERVICE_URL, _REAL_READ_BUDGET
    )

    report = watch_report(SERVICE_URL, reading, _REAL_READ_BUDGET)
    assert reading.failure is None
    assert not reading.reader_ended
    assert report.attention_feed_sample.frames_read == 1
    assert {finding.kind for finding in report.findings} == {
        WatchFindingKind.STREAM_FAILED,
        WatchFindingKind.READER_DIED,
    }


@pytest.mark.parametrize(
    "promised",
    [0, FRAME_LIMIT_BYTES + 1],
    ids=["a length of nothing", "a length beyond any record"],
)
def test_a_length_no_record_could_have_ends_the_gathering_as_corrupt(
    promised: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipe that stops making sense is named, never waited on: neither a
    length of nothing nor one beyond anything this reading writes may leave
    the reporting side waiting for bytes that would complete it."""

    monkeypatch.setenv(
        writes_a_length_no_record_has.FRAME_LENGTH_CHANNEL, str(promised)
    )

    reading = supervised_reading(
        writes_a_length_no_record_has.__name__, SERVICE_URL, _REAL_READ_BUDGET
    )

    assert reading.failure == ReaderFailure(
        ReaderPhase.READING, TransportFailureCategory.IPC_CORRUPT
    )
    assert not reading.deadline_passed
    report = watch_report(SERVICE_URL, reading, _REAL_READ_BUDGET)
    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.READER_FAILED
    assert TransportFailureCategory.IPC_CORRUPT.value in finding.detail


def test_an_interrupted_wait_still_kills_the_reading_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every step of the stopping stands in the one before it's `finally`, so
    an interruption while waiting for a reading to end on its own cannot leave
    that process running: the one that ignores being stopped is killed anyway,
    and what interrupted the wait is what comes out."""

    waited_on: list[subprocess.Popen[bytes]] = []
    wait_for_it = subprocess.Popen.wait

    def wait_that_is_interrupted_once(
        reader: subprocess.Popen[bytes], timeout: float | None = None
    ) -> int:
        waited_on.append(reader)
        if len(waited_on) == 1:
            raise KeyboardInterrupt
        return wait_for_it(reader, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait_that_is_interrupted_once)

    with pytest.raises(KeyboardInterrupt):
        supervised_reading(
            hangs_on_after_closing_the_pipe.__name__, SERVICE_URL, _REAL_READ_BUDGET
        )

    assert waited_on[0].returncode == -signal.SIGKILL


def test_an_interrupt_at_the_moment_the_reading_starts_still_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt arriving between the reading process existing and this
    process holding its handle would leave a reader nobody stops. It waits
    until the handle is bound: the interrupt still comes out of the call, and
    the process it landed on is stopped and reaped first.
    """

    started: list[subprocess.Popen[bytes]] = []
    open_a_process = subprocess.Popen

    def start_and_interrupt(
        command: Sequence[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        """The reading process, started exactly as production starts it, with
        an interrupt sent the instant it exists."""

        reader = open_a_process(
            command, stdin=stdin, stdout=stdout, stderr=stderr, pass_fds=pass_fds
        )
        started.append(reader)
        os.kill(os.getpid(), signal.SIGINT)
        return reader

    monkeypatch.setattr(subprocess, "Popen", start_and_interrupt)

    with pytest.raises(KeyboardInterrupt):
        supervised_reading(
            ignores_being_stopped.__name__, SERVICE_URL, _REAL_READ_BUDGET
        )

    assert started[0].returncode == -signal.SIGKILL


def test_a_pipe_this_process_can_no_longer_read_ends_the_gathering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the descriptor is an end to what can still arrive, never a crash
    of the process that reports: the reading is stopped and reaped, and the
    report says what a reading that delivered nothing says."""

    def set_blocking_on_a_descriptor_that_is_gone(
        descriptor: int, blocking: bool
    ) -> None:
        del descriptor, blocking
        raise OSError(errno.EBADF, "this descriptor is gone")

    monkeypatch.setattr(os, "set_blocking", set_blocking_on_a_descriptor_that_is_gone)

    reading = supervised_reading(
        ignores_being_stopped.__name__, SERVICE_URL, _REAL_READ_BUDGET
    )

    assert reading.doors == []
    assert reading.reader_reaped
    report = watch_report(SERVICE_URL, reading, _REAL_READ_BUDGET)
    assert {finding.kind for finding in report.findings} == {
        WatchFindingKind.READER_DIED
    }


def test_a_reader_that_will_not_be_stopped_is_killed_and_reported() -> None:
    """The stopping is bounded whatever the reader does: a process that
    ignores being told to stop is killed, and the report says the reading did
    not finish rather than waiting for it."""

    started = time.monotonic()
    reading = supervised_reading(
        ignores_being_stopped.__name__, SERVICE_URL, _STUBBORN_READER_BUDGET
    )
    elapsed = time.monotonic() - started

    assert reading.deadline_passed
    assert reading.reader_reaped
    assert reading.reader_exit_code == -signal.SIGKILL
    assert elapsed < (
        _STUBBORN_READER_BUDGET.deadline_seconds
        + _STOP_BUDGET_SECONDS
        + _DEADLINE_TOLERANCE_SECONDS
    )
    report = watch_report(SERVICE_URL, reading, _STUBBORN_READER_BUDGET)
    assert {finding.kind for finding in report.findings} == {
        WatchFindingKind.READING_CUT_SHORT
    }


def test_nothing_the_reading_process_touches_can_log_a_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reading process is private: nothing in it may log at all, whoever
    asks. Proven against a real reply, with a file handler hung on the very
    logger that carries a reply's headers."""

    chatter = tmp_path / "chatter.log"
    monkeypatch.setenv(logs_a_reply_to_a_file.CHATTER_LOG_CHANNEL, str(chatter))

    with _LoopbackServer(_answer_with_a_talkative_status_line) as server:
        reading = supervised_reading(
            logs_a_reply_to_a_file.__name__, server.url, _REAL_READ_BUDGET
        )

    assert reading.reader_ended
    assert len(reading.doors) == len(WATCH_ENDPOINTS) - 1
    assert chatter.read_bytes() == b""


def test_a_deadline_during_half_a_frame_keeps_every_whole_record() -> None:
    """A frame the feed only half sent is no record at all, and waiting for
    the rest of it is exactly what a deadline must not do: the frame before it
    is in the report, the reading is named as cut short, and nothing raised."""

    with _LoopbackServer(_half_a_frame_after_a_whole_one) as server:
        served_url = server.url
        reading = read_instance(served_url, _REAL_DEADLINE_BUDGET)

    report = watch_report(served_url, reading, _REAL_DEADLINE_BUDGET)
    assert reading.deadline_passed
    assert report.attention_feed_sample.frames_read == 1
    assert report.attention_feed_sample.outcome is EventSampleOutcome.INTERRUPTED
    assert EVENTS_PATH in report.endpoints_read
    assert {WatchFindingKind.STREAM_FAILED, WatchFindingKind.READING_CUT_SHORT} <= {
        finding.kind for finding in report.findings
    }
    assert reading.reader_reaped


def test_an_answer_larger_than_one_read_of_the_pipe_arrives_whole() -> None:
    """Every door answering as much as a bounded read may keep: each record is
    larger than one read of the pipe and larger than the pipe's own buffer, so
    the reporting process only ever sees pieces -- and must hand back exactly
    what the reading read."""

    with _LoopbackServer(_answer_a_body_that_fills_the_cap) as server:
        reading = read_instance(server.url, _REAL_READ_BUDGET)

    assert reading.reader_ended
    assert [
        len(door.answer.body) for door in reading.doors if isinstance(door, DoorRead)
    ] == [len(_BODY_THAT_FILLS_THE_CAP)] * (len(WATCH_ENDPOINTS) - 1)


def test_no_transport_chatter_reaches_the_output_of_this_command(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """httpx logs every request's status line -- the far side's own reason
    phrase included -- and httpcore logs a reply's headers. All of that
    happens in the reading process, which writes nowhere: its standard streams
    go to the null device and both libraries are disabled in its own logging.
    What this command prints is therefore one JSON document and nothing else,
    proven against a real reply that carries text of the server's choosing.
    """

    with _LoopbackServer(_answer_with_a_talkative_status_line) as server:
        served_url = server.url
        exit_code = execute_watch(argparse.Namespace(service=served_url))

    printed, errors = capfd.readouterr()
    assert json.loads(printed)["service_url"] == served_url
    assert _CHATTER_REASON_PHRASE not in printed + errors
    assert _CHATTER_HEADER_VALUE not in printed + errors
    assert exit_code != 0
