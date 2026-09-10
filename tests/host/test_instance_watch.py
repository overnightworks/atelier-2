"""What `atelier2 watch` reports against a served instance's fixed doors.

Most tests speak through the `transport` seam `read_instance` is built to
take -- never the live instance (#1502 forbids that; the operator's own ruling
on the observer contract decides when a real read is added). Two things a mock
cannot show run against `_LoopbackServer`, a real server on loopback that
hangs on purpose: that an interrupted read leaves no socket behind, and that
nothing the transport libraries log about a real reply reaches a handler.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import logging
import signal
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
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
    PROBLEM_MEDIA_TYPE,
    AtelierApi,
    AtelierApiTransportFailure,
    BoundedRead,
    EventFrameCollection,
    EventSampleOutcome,
    WallClockDeadlineAlreadyArmed,
    WallClockDeadlineExceeded,
    WallClockDeadlineUnavailable,
    reading_client,
    wall_clock_deadline,
)
from atelier2.host.instance_watch import (
    EVENTS_PATH,
    HEALTH_PATH,
    RUN_PATH,
    SEAT_PATH,
    TRANSPORT_LOGGER_NAMES,
    WATCH_ENDPOINTS,
    WORKFLOW_REVISIONS_PATH,
    AttentionFeedSample,
    InstanceReading,
    WatchBudget,
    WatchFinding,
    WatchFindingKind,
    WatchReport,
    execute_watch,
    read_instance,
    watch_exit_code,
    watch_instance,
    watch_report,
)

SERVICE_URL = "http://127.0.0.1:8422"
RECORDED_AT = "2026-09-10T00:00:00Z"
SEAT_TOKEN = "seat-terminal-access-token-9c41"
SEAT_ALIVE_URL = f"http://127.0.0.1:9999/terminal?token={SEAT_TOKEN}"
_TEST_BUDGET = WatchBudget(
    deadline_seconds=1.0,
    door_read_timeout_seconds=1.0,
    event_sample_read_timeout_seconds=1.0,
)
_TEST_SAMPLE = AttentionFeedSample(
    EventSampleOutcome.SILENT, frames_read=0, bytes_read=0
)
_TEST_READ_TIMEOUT_SECONDS = 1.0
_TEST_MAXIMUM_BYTES = 65_536


@pytest.fixture(autouse=True)
def _transport_logging_untouched() -> Iterator[None]:
    """`execute_watch` filters the transport libraries on the handlers of the
    process it owns; a test process is shared, so each test hands the levels,
    handlers, and handler filters it found back."""

    names = (*TRANSPORT_LOGGER_NAMES, "httpcore.http11")
    loggers = [logging.getLogger(name) for name in names]
    before = [
        (logger.level, list(logger.handlers), logger.propagate) for logger in loggers
    ]
    root_filters = [
        (handler, list(handler.filters)) for handler in logging.getLogger().handlers
    ]
    yield
    for logger, (level, handlers, propagate) in zip(loggers, before, strict=True):
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.propagate = propagate
    for handler, filters in root_filters:
        handler.filters[:] = filters


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


def _watched(overrides: dict[str, bytes | httpx.Response] | None = None) -> WatchReport:
    transport, _ = _served(overrides)
    return watch_instance(SERVICE_URL, transport=transport)


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

    report = watch_instance(
        SERVICE_URL, transport=httpx.MockTransport(refuse_to_connect)
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
    watch_instance(SERVICE_URL, transport=transport)

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
            into=BoundedRead(),
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
            into=BoundedRead(),
        )
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=1_000,
            maximum_frames=10,
            into=EventFrameCollection(),
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
            into=BoundedRead(),
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
            into=EventFrameCollection(),
        )

    assert "content-encoding" in failure.value.reason


def test_a_content_type_that_is_not_a_stream_is_refused_before_one_body_byte() -> None:
    """The decision falls on the header. An error page whose body trickles
    under every read timeout would otherwise run out the whole reading phase
    and then look like a stream that merely sent no frame."""

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
            into=EventFrameCollection(),
        )

    assert opened == []
    assert failure.value.body == b""
    assert "not an event stream" in failure.value.reason


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
def test_no_sentinel_from_any_answer_ever_reaches_the_report_an_exception_or_a_log(
    label: str,
    overrides: dict[str, bytes | httpx.Response],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole command, not only the report it builds: this drives
    `execute_watch`, the entry point that also owns dropping the transport
    libraries' own chatter, and sweeps every channel a character of an answer
    could leave through -- printed report, log records at `DEBUG`, and any
    exception that escapes with its causes.
    """

    del label
    caplog.set_level(logging.DEBUG)

    raised: Exception | None = None
    transport, _ = _served(overrides)
    try:
        execute_watch(argparse.Namespace(service=SERVICE_URL), transport=transport)
    except Exception as error:  # noqa: BLE001 -- the sweep itself, not a caller
        raised = error

    haystack = caplog.text + capsys.readouterr().out
    cause: BaseException | None = raised
    while cause is not None:
        haystack += str(cause)
        cause = cause.__cause__

    assert _LEAK_SENTINEL not in haystack


@pytest.mark.parametrize(
    ("label", "overrides"),
    _POISONED_FIXTURES,
    ids=[label for label, _ in _POISONED_FIXTURES],
)
def test_no_sentinel_survives_in_a_failure_the_watcher_itself_consumes(
    label: str, overrides: dict[str, bytes | httpx.Response]
) -> None:
    """The sweep above only sees exceptions that escape; the watcher swallows
    most of them into findings. This reads the same answers at the client
    boundary the watcher reads them through and inspects the failure it
    hands over: its message, its reason, and its whole `__cause__` chain --
    which must be empty, because a chained library exception carries the far
    side's own text into every traceback that ever prints it.

    `failure.body` is deliberately not swept: those are the answer's own
    bytes, handed over so the watcher can recognize a problem document in
    them, and no channel ever prints them.
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
                    into=BoundedRead(),
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
                into=EventFrameCollection(),
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
            into=BoundedRead(),
        )

    assert raised.value.reason == f"transport failure: {category}"
    assert raised.value.__cause__ is None
    assert _LEAK_SENTINEL not in raised.value.reason


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
            into=BoundedRead(),
        )

    (response,) = responses
    assert response.is_closed


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
        instance_watch, "watch_instance", lambda service_url, *, transport: canned
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


def test_a_silent_event_feed_stops_the_sample_instead_of_hanging() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        def body() -> Iterator[bytes]:
            yield b": lead-in\n\n"
            raise httpx.ReadTimeout("no data arrived", request=request)

        return _feed(body())

    collected = EventFrameCollection()
    with AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api:
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=0.01,
            maximum_bytes=1_000,
            maximum_frames=5,
            into=collected,
        )

    assert collected.outcome is EventSampleOutcome.SILENT
    assert collected.frames == []


def test_a_stream_that_closes_before_any_budget_is_hit_is_named_closed_early() -> None:
    collected = EventFrameCollection()
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
            into=collected,
        )

    assert collected.frames == ['{"event": "AGENT_COMPLETED"}']
    assert collected.outcome is EventSampleOutcome.CLOSED_EARLY


def test_reading_stops_at_the_frame_limit_without_hanging() -> None:
    content = _sse(*(f'{{"n": {n}}}' for n in range(5)))
    collected = EventFrameCollection()
    with AtelierApi(
        SERVICE_URL, transport=httpx.MockTransport(lambda request: _feed(content))
    ) as api:
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=1_000_000,
            maximum_frames=2,
            into=collected,
        )

    assert len(collected.frames) == 2
    assert collected.outcome is EventSampleOutcome.FRAME_LIMIT


def test_reading_stops_at_the_byte_limit_without_hanging() -> None:
    chunks = (b"data: a\n\n", b"data: b\n\n", b"data: c\n\n")
    collected = EventFrameCollection()
    with AtelierApi(
        SERVICE_URL, transport=httpx.MockTransport(lambda request: _feed(iter(chunks)))
    ) as api:
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=len(chunks[0]),
            maximum_frames=100,
            into=collected,
        )

    assert collected.frames == ["a"]
    assert collected.outcome is EventSampleOutcome.BYTE_LIMIT


def test_an_already_passed_deadline_reads_nothing_not_even_the_request() -> None:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, content=b"{}")

    with (
        pytest.raises(WallClockDeadlineExceeded),
        reading_client(SERVICE_URL, 0.0, transport=httpx.MockTransport(handle)) as api,
    ):
        api.bounded_get(
            HEALTH_PATH,
            read_timeout_seconds=_TEST_READ_TIMEOUT_SECONDS,
            maximum_bytes=1_000,
            into=BoundedRead(),
        )

    assert asked == []


def test_a_deadline_this_thread_cannot_be_given_is_refused_rather_than_skipped() -> (
    None
):
    """Only the main thread receives the process's alarm. Anywhere else the
    deadline says so, instead of running an unbounded read that merely looks
    bounded."""

    refusals: list[BaseException] = []

    def ask_off_the_main_thread() -> None:
        try:
            with wall_clock_deadline(_DEADLINE_SECONDS):
                pass
        except BaseException as refusal:  # noqa: BLE001 -- carried to the assert
            refusals.append(refusal)

    asking = threading.Thread(target=ask_off_the_main_thread)
    asking.start()
    asking.join(_SERVER_JOIN_SECONDS)

    assert not asking.is_alive()
    (refusal,) = refusals
    assert isinstance(refusal, WallClockDeadlineUnavailable)


def test_a_second_deadline_is_refused_while_one_is_already_running() -> None:
    """One process has one interval timer: a nested deadline would replace the
    outer one's expiry and hand its own handler back on the way out, leaving
    the block that believed itself bounded running with no alarm at all."""

    with (
        wall_clock_deadline(_SERVER_JOIN_SECONDS),
        pytest.raises(WallClockDeadlineAlreadyArmed),
        wall_clock_deadline(_SERVER_JOIN_SECONDS),
    ):
        pytest.fail("a nested deadline must never arm")


def test_an_alarm_landing_during_restoration_still_leaves_the_previous_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal queued when the timer is disarmed is delivered at the next
    bytecode boundary, which can fall inside the restoring call itself. That
    throw must not escape, and it must not leave this module's own handler
    installed for whatever the process does next."""

    previous = signal.getsignal(signal.SIGALRM)
    real_signal = signal.signal
    landed: list[bool] = []

    def signal_with_one_late_alarm(
        number: int, handler: object
    ) -> object:  # pragma: no cover - argument shapes come from signal itself
        if handler is previous and not landed:
            landed.append(True)
            raise WallClockDeadlineExceeded
        return real_signal(number, handler)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(signal, "signal", signal_with_one_late_alarm)

    with wall_clock_deadline(_SERVER_JOIN_SECONDS):
        pass

    assert landed == [True]
    assert signal.getsignal(signal.SIGALRM) is previous


_DEADLINE_SECONDS = 0.5
"""What every deadline proof below gives the client to finish in."""

_DEADLINE_TOLERANCE_SECONDS = 1.5
"""How much later than its deadline a call may still return and count as
bounded -- generous, because what is proven is that it returns at all, not
how promptly a shared machine schedules it."""

_PATIENT_READ_TIMEOUT_SECONDS = 30.0
"""Far past the deadline on purpose: whatever ends these calls, it is not
httpx's own per-read timeout."""

_DRIBBLE_INTERVAL_SECONDS = 0.02
_ACCEPT_POLL_SECONDS = 0.05
_SERVER_JOIN_SECONDS = 5.0

_CHATTER_REASON_PHRASE = "SENTINEL-REASON-PHRASE"
_CHATTER_HEADER_VALUE = "SENTINEL-HEADER-VALUE"


class _LoopbackServer:
    """A real TCP server on loopback that hangs on purpose.

    `httpx.MockTransport` cannot stand in for this: a mock never opens a
    socket, so nothing it does can show that a deadline reaching a blocked
    read leaves no connection behind, nor what the transport libraries log
    about a real reply. `speak` is what this server does with an accepted
    connection; afterwards it reads until the client closes, which is what
    `connections_closed_by_client` counts -- a peer's own FIN, the
    socket-level proof that the interrupted call cleaned up after itself.
    """

    def __init__(self, speak: Callable[[socket.socket, threading.Event], None]) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._speak = speak
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="loopback-server")
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
    connection.settimeout(_SERVER_JOIN_SECONDS)
    try:
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


def _stay_silent(connection: socket.socket, stop: threading.Event) -> None:
    """Accept the request and answer nothing at all -- not one header byte."""

    del connection, stop


def _dribble_header_bytes(connection: socket.socket, stop: threading.Event) -> None:
    """A reply whose headers never finish: one byte at a time, always sooner
    than any read timeout, forever. Only an absolute deadline ends this."""

    try:
        connection.sendall(b"HTTP/1.1 200 OK\r\nX-Filler: ")
        while not stop.wait(_DRIBBLE_INTERVAL_SECONDS):
            connection.sendall(b"x")
    except OSError:
        return


def _answer_with_a_talkative_status_line(
    connection: socket.socket, stop: threading.Event
) -> None:
    """A complete reply whose status line and headers carry text of the far
    side's own choosing -- exactly what httpx logs at `INFO` and httpcore at
    `DEBUG`. `Connection: close` keeps one request to one connection, so this
    one-at-a-time server answers each of them."""

    del stop
    body = b"{}"
    head = (
        f"HTTP/1.1 200 {_CHATTER_REASON_PHRASE}\r\n"
        f"X-Sentinel: {_CHATTER_HEADER_VALUE}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    try:
        connection.sendall(head + body)
    except OSError:
        return


def test_a_connection_that_never_answers_ends_at_its_deadline_and_closes() -> None:
    """The whole point of the deadline: a real socket that accepts and then
    says nothing. httpx's read timeout is set far beyond the deadline, so
    only the deadline can end this -- and the server sees the client's own
    close before the client is even out of scope, which is the proof that the
    interrupted read cleaned up rather than a later `close()` doing it."""

    threads_before = set(threading.enumerate())
    with _LoopbackServer(_stay_silent) as server:
        started = time.monotonic()
        with (
            pytest.raises(WallClockDeadlineExceeded),
            reading_client(server.url, _DEADLINE_SECONDS) as api,
        ):
            api.bounded_get(
                HEALTH_PATH,
                read_timeout_seconds=_PATIENT_READ_TIMEOUT_SECONDS,
                maximum_bytes=1_000,
                into=BoundedRead(),
            )
        elapsed = time.monotonic() - started
        assert _waited_for(lambda: server.connections_closed_by_client == 1)

    assert elapsed < _DEADLINE_SECONDS + _DEADLINE_TOLERANCE_SECONDS
    assert set(threading.enumerate()) == threads_before


def test_no_further_request_is_made_after_the_deadline_has_passed() -> None:
    """A reply that keeps trickling header bytes never trips a read timeout at
    all: every gap is shorter than one. Only the deadline ends it -- and the
    reading phase ends with it, so the server is never asked again."""

    threads_before = set(threading.enumerate())
    with _LoopbackServer(_dribble_header_bytes) as server:
        report = _read_with_budgets(
            server.url,
            deadline_seconds=_DEADLINE_SECONDS,
            door_read_timeout_seconds=_PATIENT_READ_TIMEOUT_SECONDS,
        )
        assert _waited_for(lambda: server.connections_accepted == 1)

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.READING_CUT_SHORT
    assert finding.endpoint == HEALTH_PATH
    assert report.endpoints_read == (HEALTH_PATH,)
    assert watch_exit_code(report) != 0
    assert set(threading.enumerate()) == threads_before


def test_the_whole_report_survives_an_instance_that_answers_nothing() -> None:
    """Every fixed door against a server that accepts and then says nothing:
    each one's own read timeout ends it well inside the phase deadline, so all
    five are reported unreachable, on one client, leaving no connection and no
    thread behind."""

    threads_before = set(threading.enumerate())
    with _LoopbackServer(_stay_silent) as server:
        report = _read_with_budgets(
            server.url,
            deadline_seconds=_SERVER_JOIN_SECONDS,
            door_read_timeout_seconds=_DEADLINE_SECONDS / 2,
        )
        assert _waited_for(
            lambda: server.connections_closed_by_client == len(WATCH_ENDPOINTS)
        )

    assert {finding.endpoint for finding in report.findings} == set(WATCH_ENDPOINTS)
    assert all(
        finding.kind == WatchFindingKind.SERVICE_UNREACHABLE
        for finding in report.findings
    )
    assert set(threading.enumerate()) == threads_before


def test_a_failure_frame_read_before_the_deadline_still_reaches_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feed that publishes a `STREAM_FAILED` frame and then hangs: the
    deadline ends the whole reading phase, and the report is still built from
    what that phase had collected -- the finding, and that the read was cut
    short."""

    failure_frame = StreamFailureResource(
        problem=problem_resource("durable-state-corrupt")
    ).model_dump_json()

    def frame_then_hang(request: httpx.Request) -> httpx.Response:
        del request

        def content() -> Iterator[bytes]:
            yield _sse(failure_frame)
            while True:
                time.sleep(_DRIBBLE_INTERVAL_SECONDS)

        return _feed(content())

    healthy, _ = _served()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.removeprefix(API_PREFIX) == EVENTS_PATH:
            return frame_then_hang(request)
        return healthy.handle_request(request)

    monkeypatch.setattr(instance_watch, "WATCH_DEADLINE_SECONDS", _DEADLINE_SECONDS)
    monkeypatch.setattr(
        instance_watch,
        "EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS",
        _PATIENT_READ_TIMEOUT_SECONDS,
    )
    report = watch_instance(SERVICE_URL, transport=httpx.MockTransport(handle))

    kinds = {finding.kind for finding in report.findings}
    assert WatchFindingKind.STREAM_FAILED in kinds
    assert WatchFindingKind.READING_CUT_SHORT in kinds
    assert report.attention_feed_sample.frames_read == 1
    assert report.attention_feed_sample.outcome is EventSampleOutcome.INTERRUPTED
    assert watch_exit_code(report) != 0


def _read_with_budgets(
    service_url: str, *, deadline_seconds: float, door_read_timeout_seconds: float
) -> WatchReport:
    """One real read of `service_url` on budgets a test can wait for."""

    with pytest.MonkeyPatch.context() as budgets:
        budgets.setattr(instance_watch, "WATCH_DEADLINE_SECONDS", deadline_seconds)
        budgets.setattr(
            instance_watch, "REQUEST_TIMEOUT_SECONDS", door_read_timeout_seconds
        )
        budgets.setattr(
            instance_watch,
            "EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS",
            door_read_timeout_seconds,
        )
        return watch_instance(service_url)


def test_the_transport_libraries_chatter_never_reaches_a_handler(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A level on the `httpcore` logger does not hold: a record made on
    `httpcore.http11` is filtered by that child's own level and then walks
    straight past its ancestors' levels to their handlers. The command drops
    the family on the handler instead -- proven against a real reply whose
    status line and headers carry text of the far side's own choosing."""

    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpcore.http11").setLevel(logging.DEBUG)

    with _LoopbackServer(_answer_with_a_talkative_status_line) as server:
        with AtelierApi(server.url) as unfiltered:
            unfiltered.get(HEALTH_PATH, timeout=_SERVER_JOIN_SECONDS)
        assert [record for record in caplog.records if record.name.startswith("httpc")]
        caplog.clear()

        execute_watch(argparse.Namespace(service=server.url))

    haystack = caplog.text + capsys.readouterr().out
    assert _CHATTER_REASON_PHRASE not in haystack
    assert _CHATTER_HEADER_VALUE not in haystack
    assert not [record for record in caplog.records if record.name.startswith("httpc")]


def test_a_reading_the_deadline_cut_short_reports_what_it_had() -> None:
    """The reporting phase is pure and runs on whatever the reading phase
    collected: a frame already read becomes its finding, and the cut is named
    beside it."""

    failure_frame = StreamFailureResource(
        problem=problem_resource("durable-state-corrupt")
    ).model_dump_json()
    reading = InstanceReading(deadline_passed=True)
    reading.feed.frames.append(failure_frame)
    reading.feed.bytes_read = len(failure_frame)
    reading.feed.outcome = EventSampleOutcome.INTERRUPTED

    report = watch_report(SERVICE_URL, reading)

    assert {finding.kind for finding in report.findings} == {
        WatchFindingKind.STREAM_FAILED,
        WatchFindingKind.READING_CUT_SHORT,
    }
    assert report.attention_feed_sample.frames_read == 1


def test_a_reading_phase_hands_back_what_it_collected_even_unfinished() -> None:
    """`read_instance` returns rather than raising when its deadline falls, so
    the reporting phase always has something to report."""

    def hang(request: httpx.Request) -> httpx.Response:
        del request
        time.sleep(_SERVER_JOIN_SECONDS)
        return httpx.Response(200, content=b"{}")

    with pytest.MonkeyPatch.context() as budgets:
        budgets.setattr(instance_watch, "WATCH_DEADLINE_SECONDS", _DEADLINE_SECONDS)
        reading = read_instance(SERVICE_URL, transport=httpx.MockTransport(hang))

    assert reading.deadline_passed
    assert [door.endpoint for door in reading.doors] == [HEALTH_PATH]
    assert not reading.doors[0].answered
