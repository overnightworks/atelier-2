"""What `atelier2 watch` reports against a served instance's fixed doors.

Every test speaks through `AtelierApi` with an `httpx.MockTransport`, exactly
the seam `watch_instance` is built to take a caller's own client through --
never the live instance (#1502 forbids that; the operator's own ruling on the
observer contract decides when a real read is added).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator

import httpx

from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import problem_resource
from atelier2.api.seat import SeatState
from atelier2.api.wire.resources import (
    DurableStateCorruptProblemResource,
    HealthResource,
    RedeployBlockedResource,
    RunProjectionCorruptResource,
    SeatResource,
    StreamFailureResource,
)
from atelier2.host.atelier_api_client import AtelierApi, EventSampleLimit
from atelier2.host.instance_watch import (
    EVENTS_PATH,
    HEALTH_PATH,
    RUN_PATH,
    SEAT_PATH,
    WATCH_ENDPOINTS,
    WORKFLOW_REVISIONS_PATH,
    WatchFinding,
    WatchFindingKind,
    WatchReport,
    watch_exit_code,
    watch_instance,
)

SERVICE_URL = "http://127.0.0.1:8422"
RECORDED_AT = "2026-09-10T00:00:00Z"
SEAT_TOKEN = "seat-terminal-access-token-9c41"
SEAT_ALIVE_URL = f"http://127.0.0.1:9999/terminal?token={SEAT_TOKEN}"

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


def test_watch_exit_code_is_zero_only_without_findings() -> None:
    clean = WatchReport(
        service_url=SERVICE_URL, endpoints_read=WATCH_ENDPOINTS, findings=()
    )
    dirty = WatchReport(
        service_url=SERVICE_URL,
        endpoints_read=WATCH_ENDPOINTS,
        findings=(WatchFinding(WatchFindingKind.SEAT_NOT_ALIVE, SEAT_PATH, "MISSING"),),
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


def test_reading_stops_at_the_overall_deadline_without_a_sleep() -> None:
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
    assert len(sample.frames) == 1
