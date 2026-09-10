"""What `atelier2 watch` reports against a served instance's fixed doors.

Every test speaks through `AtelierApi` with an `httpx.MockTransport`, exactly
the seam `watch_instance` is built to take a caller's own client through --
never the live instance (#1502 forbids that; the operator's own ruling on the
observer contract decides when a real read is added).
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import itertools
import json
from collections.abc import Iterator

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
from atelier2.host import atelier_api_client, instance_watch
from atelier2.host.atelier_api_client import AtelierApi, EventSampleLimit
from atelier2.host.instance_watch import (
    EVENTS_PATH,
    HEALTH_PATH,
    RUN_PATH,
    SEAT_PATH,
    WATCH_ENDPOINTS,
    WORKFLOW_REVISIONS_PATH,
    WatchBudget,
    WatchFinding,
    WatchFindingKind,
    WatchReport,
    execute_watch,
    watch_exit_code,
    watch_instance,
)

SERVICE_URL = "http://127.0.0.1:8422"
RECORDED_AT = "2026-09-10T00:00:00Z"
SEAT_TOKEN = "seat-terminal-access-token-9c41"
SEAT_ALIVE_URL = f"http://127.0.0.1:9999/terminal?token={SEAT_TOKEN}"
_TEST_BUDGET = WatchBudget(deadline_seconds=1.0, read_timeout_seconds=1.0)

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

    return httpx.Response(200, content=content())


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
    with AtelierApi(SERVICE_URL, transport=transport) as api:
        return watch_instance(SERVICE_URL, api=api)


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


def test_a_run_projection_corrupt_frame_after_a_healthy_one_is_reported_with_its_run() -> (
    None
):
    corrupt = RunProjectionCorruptResource(
        public_run_reference="run1.aGVhbHRoeQ",
        problem=_DURABLE_STATE_CORRUPT_PROBLEM,
    )
    report = _watched(
        {EVENTS_PATH: _sse('{"event": "AGENT_COMPLETED"}', corrupt.model_dump_json())}
    )

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.RUN_PROJECTION_CORRUPT
    assert finding.public_run_reference == "run1.aGVhbHRoeQ"


def test_health_redeploy_blocked_is_reported_and_its_absence_is_clean() -> None:
    blocked = HealthResource(
        status="serving",
        source_commit="a" * 40,
        source_tree="b" * 40,
        serve_started_at=RECORDED_AT,
        redeploy=RedeployBlockedResource(blocked_since=RECORDED_AT, reason="stuck"),
    )
    report = _watched({HEALTH_PATH: blocked.model_dump_json().encode()})

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.REDEPLOY_BLOCKED
    assert "stuck" in finding.detail


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

    with AtelierApi(
        SERVICE_URL, transport=httpx.MockTransport(refuse_to_connect)
    ) as api:
        report = watch_instance(SERVICE_URL, api=api)

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
    with AtelierApi(SERVICE_URL, transport=transport) as api:
        watch_instance(SERVICE_URL, api=api)

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

    report = _watched({EVENTS_PATH: httpx.Response(200, content=_sse(body))})

    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.RESPONSE_REFUSED
    assert finding.endpoint == EVENTS_PATH


def test_a_raw_problem_document_on_the_event_stream_is_reported() -> None:
    """A route caught before it ever starts streaming answers its own
    content type and a plain JSON body -- not one `data:` line in sight."""

    body = problem_resource("internal-error").model_dump_json().encode()
    raw = httpx.Response(
        200, content=body, headers={"content-type": "application/problem+json"}
    )

    report = _watched({EVENTS_PATH: raw})

    assert report.attention_feed_sample == "refused"
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
    report = _watched({EVENTS_PATH: httpx.Response(200, content=content)})

    assert report.attention_feed_sample in (
        "frame-limit",
        "byte-limit",
        "overall-deadline",
        "silent",
        "closed-early",
    )


def test_a_feed_that_closes_before_sending_anything_is_a_finding() -> None:
    report = _watched({EVENTS_PATH: httpx.Response(200, content=b"")})

    assert report.attention_feed_sample == "closed-early"
    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.STREAM_CLOSED_EARLY
    assert finding.endpoint == EVENTS_PATH
    assert "before it ever sent a byte" in finding.detail


def test_a_feed_that_sends_only_comments_then_closes_is_named_by_bytes_not_frames() -> (
    None
):
    """No frame does not mean no bytes: a heartbeat comment (no `data:`
    line, so no frame ever assembles) followed by EOF must not be reported
    as a connection that "never sent a byte" -- it sent one, just not a
    data frame."""

    report = _watched({EVENTS_PATH: httpx.Response(200, content=b": heartbeat\n\n")})

    assert report.attention_feed_sample == "closed-early"
    (finding,) = report.findings
    assert finding.kind == WatchFindingKind.STREAM_CLOSED_EARLY
    assert "without ever completing a data frame" in finding.detail
    assert "before it ever sent a byte" not in finding.detail


def test_a_quiet_feed_that_stays_open_is_not_itself_a_finding() -> None:
    report = _watched()

    assert report.attention_feed_sample == "silent"
    assert report.findings == ()


def test_bounded_get_never_exceeds_its_byte_cap_without_hanging() -> None:
    content = b"x" * 10_000

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content)

    with (
        AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api,
        pytest.raises(atelier_api_client.AtelierApiTransportFailure),
    ):
        api.bounded_get(
            HEALTH_PATH,
            request_timeout_seconds=1.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=100,
        )


def test_bounded_reads_ask_for_identity_encoding() -> None:
    seen: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, content=b"{}")

    with AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api:
        api.bounded_get(
            HEALTH_PATH,
            request_timeout_seconds=1.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=1_000,
        )
        api.sampled_event_frames(
            EVENTS_PATH,
            accept="text/event-stream",
            request_timeout_seconds=1.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=1_000,
            maximum_frames=10,
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
        pytest.raises(atelier_api_client.AtelierApiTransportFailure) as failure,
    ):
        api.bounded_get(
            HEALTH_PATH,
            request_timeout_seconds=1.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=4,
        )

    assert "content-encoding" in failure.value.reason
    assert failure.value.body == b""


def test_a_compressed_event_stream_reply_is_refused_before_being_read() -> None:
    real_gzip = gzip.compress(b"data: hello\n\n")

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, content=real_gzip, headers={"content-encoding": "gzip"}
        )

    with (
        AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api,
        pytest.raises(atelier_api_client.AtelierApiTransportFailure) as failure,
    ):
        api.sampled_event_frames(
            EVENTS_PATH,
            accept="text/event-stream",
            request_timeout_seconds=1.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=1_000,
            maximum_frames=10,
        )

    assert "content-encoding" in failure.value.reason


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
        pytest.raises(atelier_api_client.AtelierApiTransportFailure),
    ):
        api.bounded_get(
            HEALTH_PATH,
            request_timeout_seconds=1.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=250,
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
        attention_feed_sample="silent",
        endpoint_budget=_TEST_BUDGET,
        event_sample_budget=_TEST_BUDGET,
    )
    monkeypatch.setattr(instance_watch, "watch_instance", lambda service_url: canned)

    exit_code = execute_watch(argparse.Namespace(service=SERVICE_URL))

    printed = json.loads(capsys.readouterr().out)
    assert printed["service_url"] == SERVICE_URL
    assert printed["attention_feed_sample"] == "silent"
    assert printed["endpoint_budget"] == {
        "deadline_seconds": 1.0,
        "read_timeout_seconds": 1.0,
        "worst_case_seconds": 2.0,
    }
    assert printed["findings"] == [
        {
            "kind": "SEAT_NOT_ALIVE",
            "endpoint": SEAT_PATH,
            "detail": "MISSING",
            "public_run_reference": None,
        }
    ]
    assert exit_code != 0


def test_watch_exit_code_is_zero_only_without_findings() -> None:
    clean = WatchReport(
        service_url=SERVICE_URL,
        endpoints_read=WATCH_ENDPOINTS,
        findings=(),
        attention_feed_sample="silent",
        endpoint_budget=_TEST_BUDGET,
        event_sample_budget=_TEST_BUDGET,
    )
    dirty = WatchReport(
        service_url=SERVICE_URL,
        endpoints_read=WATCH_ENDPOINTS,
        findings=(WatchFinding(WatchFindingKind.SEAT_NOT_ALIVE, SEAT_PATH, "MISSING"),),
        attention_feed_sample="silent",
        endpoint_budget=_TEST_BUDGET,
        event_sample_budget=_TEST_BUDGET,
    )

    assert watch_exit_code(clean) == 0
    assert watch_exit_code(dirty) != 0


def test_a_silent_event_feed_stops_the_sample_instead_of_hanging() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        def body() -> Iterator[bytes]:
            yield b": lead-in\n\n"
            raise httpx.ReadTimeout("no data arrived", request=request)

        return httpx.Response(200, content=body())

    with AtelierApi(SERVICE_URL, transport=httpx.MockTransport(handle)) as api:
        sample = api.sampled_event_frames(
            EVENTS_PATH,
            accept="text/event-stream",
            request_timeout_seconds=0.01,
            overall_deadline_seconds=1.0,
            maximum_bytes=1_000,
            maximum_frames=5,
        )

    assert sample.stopped is EventSampleLimit.SILENT
    assert sample.frames == ()


def test_a_stream_that_closes_before_any_budget_is_hit_is_named_by_no_stop_reason() -> (
    None
):
    with AtelierApi(
        SERVICE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=_sse('{"event": "AGENT_COMPLETED"}')
            )
        ),
    ) as api:
        sample = api.sampled_event_frames(
            EVENTS_PATH,
            accept="text/event-stream",
            request_timeout_seconds=1.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=1_000_000,
            maximum_frames=100,
        )

    assert sample.frames == ('{"event": "AGENT_COMPLETED"}',)
    assert sample.stopped is None


def test_reading_stops_at_the_frame_limit_without_hanging() -> None:
    content = _sse(*(f'{{"n": {n}}}' for n in range(5)))
    with AtelierApi(
        SERVICE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=content)
        ),
    ) as api:
        sample = api.sampled_event_frames(
            EVENTS_PATH,
            accept="text/event-stream",
            request_timeout_seconds=1.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=1_000_000,
            maximum_frames=2,
        )

    assert len(sample.frames) == 2
    assert sample.stopped is EventSampleLimit.FRAME_LIMIT


def test_reading_stops_at_the_byte_limit_without_hanging() -> None:
    chunks = (b"data: a\n\n", b"data: b\n\n", b"data: c\n\n")
    with AtelierApi(
        SERVICE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=iter(chunks))
        ),
    ) as api:
        sample = api.sampled_event_frames(
            EVENTS_PATH,
            accept="text/event-stream",
            request_timeout_seconds=1.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=len(chunks[0]),
            maximum_frames=100,
        )

    assert sample.frames == ("a",)
    assert sample.stopped is EventSampleLimit.BYTE_LIMIT


def test_an_already_passed_deadline_reads_nothing_not_even_the_request() -> None:
    content = _sse(*(f'{{"n": {n}}}' for n in range(5)))
    with AtelierApi(
        SERVICE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=content)
        ),
    ) as api:
        sample = api.sampled_event_frames(
            EVENTS_PATH,
            accept="text/event-stream",
            request_timeout_seconds=1.0,
            overall_deadline_seconds=0.0,
            maximum_bytes=1_000_000,
            maximum_frames=100,
        )

    assert sample.stopped is EventSampleLimit.OVERALL_DEADLINE
    assert sample.frames == ()


def test_a_transport_that_never_delivers_is_stopped_by_the_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sleep, no real time passing: a controlled fake clock proves the
    deadline check itself is what stops a source that would otherwise
    produce chunks forever -- not a lucky end of content."""

    def never_ending_chunks() -> Iterator[bytes]:
        while True:
            yield b": heartbeat\n\n"

    with AtelierApi(
        SERVICE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=never_ending_chunks())
        ),
    ) as api:
        clock = itertools.chain([0.0, 0.0], itertools.repeat(100.0))
        monkeypatch.setattr(atelier_api_client.time, "monotonic", lambda: next(clock))

        sample = api.sampled_event_frames(
            EVENTS_PATH,
            accept="text/event-stream",
            request_timeout_seconds=5.0,
            overall_deadline_seconds=1.0,
            maximum_bytes=1_000_000,
            maximum_frames=1_000_000,
        )

    assert sample.stopped is EventSampleLimit.OVERALL_DEADLINE
    assert sample.frames == ()
