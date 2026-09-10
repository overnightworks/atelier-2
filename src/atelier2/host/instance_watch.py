"""`atelier2 watch`: read a served instance's fixed doors and report what it saw.

This is a read-only observer, not a client of any one feature: it asks the
same doors an operator or the cockpit already asks -- health, seat, the run
and catalog listings, and a bounded sample of the attention feed -- and turns
what each answered into a typed finding. Nothing here starts, cancels, or
publishes anything, and nothing it reads ever reaches the report unredacted:
the seat's terminal address carries the terminal's own access token
(`served_seat.py`), so only the seat's state is ever named.

Every call this command makes is one GET, on one path from `WATCH_ENDPOINTS`,
answered or refused within a fixed budget -- the attention feed is sampled,
never followed, because it is documented to never end on its own.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import ValidationError

from atelier2.api.seat import SeatState
from atelier2.api.wire.resources import (
    DurableStateCorruptProblemResource,
    HealthResource,
    ProblemResource,
    RunProjectionCorruptResource,
    SeatResource,
    StreamFailureResource,
)
from atelier2.host.address import DEFAULT_SERVICE_URL
from atelier2.host.atelier_api_client import (
    AtelierApi,
    AtelierApiAddressUnusable,
    AtelierApiTransportFailure,
    opened_api,
)
from atelier2.host.run_command import (
    EVENT_STREAM_MEDIA_TYPE,
    RUN_PATH,
    STREAM_FAILURE_NAME,
)

WATCH_DESCRIPTION = """\
Read a served Atelier instance's fixed doors and report typed findings.

Offline in the sense migrate and connect are: this changes nothing. It asks
health, seat, the runs and workflow-revisions listings, and a bounded sample
of the attention feed, then prints one JSON report to stdout -- empty and
exit 0 when nothing was found, non-empty and a non-zero exit otherwise.
"""

REQUEST_TIMEOUT_SECONDS: Final = 5.0
"""How long one plain GET among the fixed endpoints may take."""

EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS: Final = 2.0
"""The read timeout on every chunk of the attention-feed sample; a silent
feed stops the sample rather than the whole command."""

EVENT_SAMPLE_DEADLINE_SECONDS: Final = 3.0
"""The whole attention-feed sample's wall-clock budget."""

EVENT_SAMPLE_MAXIMUM_BYTES: Final = 65_536
EVENT_SAMPLE_MAXIMUM_FRAMES: Final = 20

HEALTH_PATH = "/health"
SEAT_PATH = "/seat"
WORKFLOW_REVISIONS_PATH = "/workflow-revisions"
EVENTS_PATH = "/events"

WATCH_ENDPOINTS: Final[tuple[str, ...]] = (
    HEALTH_PATH,
    SEAT_PATH,
    RUN_PATH,
    WORKFLOW_REVISIONS_PATH,
    EVENTS_PATH,
)
"""The fixed, complete list of paths one `watch` call reads -- one GET each,
no retry, no pagination; a page beyond the first is each list endpoint's
own paging defect class, watched for elsewhere (#1313, #1501), not here."""

RUN_PROJECTION_CORRUPT_NAME: Final[str] = RunProjectionCorruptResource.model_fields[
    "event"
].default


class WatchFindingKind(StrEnum):
    """Every shape one `watch` call can report; absence of all of them is clean."""

    RESPONSE_REFUSED = "RESPONSE_REFUSED"
    SERVICE_UNREACHABLE = "SERVICE_UNREACHABLE"
    STREAM_FAILED = STREAM_FAILURE_NAME
    RUN_PROJECTION_CORRUPT = RUN_PROJECTION_CORRUPT_NAME
    STREAM_SAMPLE_UNREADABLE = "STREAM_SAMPLE_UNREADABLE"
    SEAT_NOT_ALIVE = "SEAT_NOT_ALIVE"
    REDEPLOY_BLOCKED = "REDEPLOY_BLOCKED"


@dataclass(frozen=True, slots=True)
class WatchFinding:
    """One typed observation, named the same way whichever endpoint raised it."""

    kind: WatchFindingKind
    endpoint: str
    detail: str
    public_run_reference: str | None = None


@dataclass(frozen=True, slots=True)
class WatchReport:
    service_url: str
    endpoints_read: tuple[str, ...]
    findings: tuple[WatchFinding, ...]


def watch_instance(service_url: str, *, api: AtelierApi | None = None) -> WatchReport:
    """Read every fixed endpoint once and report what each answered.

    `api` is a test seam: a caller already holding one client -- an injected
    `httpx.MockTransport` double, most often -- hands it in and keeps owning
    its lifetime. Production composition leaves it unset, opening and closing
    its own client for this one call.
    """

    if api is not None:
        return _watched(service_url, api)
    with opened_api(service_url) as opened:
        return _watched(service_url, opened)


def _watched(service_url: str, api: AtelierApi) -> WatchReport:
    findings: list[WatchFinding] = [
        *_health_findings(api),
        *_seat_findings(api),
        *_listing_findings(api, RUN_PATH),
        *_listing_findings(api, WORKFLOW_REVISIONS_PATH),
        *_event_sample_findings(api),
    ]
    return WatchReport(
        service_url=service_url,
        endpoints_read=WATCH_ENDPOINTS,
        findings=tuple(findings),
    )


def _health_findings(api: AtelierApi) -> tuple[WatchFinding, ...]:
    try:
        answered = api.get(HEALTH_PATH, timeout=REQUEST_TIMEOUT_SECONDS)
    except AtelierApiTransportFailure as failure:
        return (_refusal_finding(HEALTH_PATH, failure),)
    try:
        health = HealthResource.model_validate_json(answered)
    except ValidationError as error:
        return (_unreadable_finding(HEALTH_PATH, error),)
    if health.redeploy is None:
        return ()
    return (
        WatchFinding(
            WatchFindingKind.REDEPLOY_BLOCKED,
            HEALTH_PATH,
            f"blocked since {health.redeploy.blocked_since}: {health.redeploy.reason}",
        ),
    )


def _seat_findings(api: AtelierApi) -> tuple[WatchFinding, ...]:
    try:
        answered = api.get(SEAT_PATH, timeout=REQUEST_TIMEOUT_SECONDS)
    except AtelierApiTransportFailure as failure:
        return (_refusal_finding(SEAT_PATH, failure),)
    try:
        seat = SeatResource.model_validate_json(answered)
    except ValidationError as error:
        return (_unreadable_finding(SEAT_PATH, error),)
    if seat.state is SeatState.ALIVE:
        return ()
    # Never the address `seat.url` may carry: that would hand the terminal's
    # own access token to anything reading this report.
    return (WatchFinding(WatchFindingKind.SEAT_NOT_ALIVE, SEAT_PATH, seat.state.value),)


def _listing_findings(api: AtelierApi, endpoint: str) -> tuple[WatchFinding, ...]:
    try:
        api.get(endpoint, timeout=REQUEST_TIMEOUT_SECONDS)
    except AtelierApiTransportFailure as failure:
        return (_refusal_finding(endpoint, failure),)
    return ()


def _event_sample_findings(api: AtelierApi) -> tuple[WatchFinding, ...]:
    try:
        sample = api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            request_timeout_seconds=EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS,
            overall_deadline_seconds=EVENT_SAMPLE_DEADLINE_SECONDS,
            maximum_bytes=EVENT_SAMPLE_MAXIMUM_BYTES,
            maximum_frames=EVENT_SAMPLE_MAXIMUM_FRAMES,
        )
    except AtelierApiTransportFailure as failure:
        return (_refusal_finding(EVENTS_PATH, failure),)
    return tuple(
        finding for frame in sample.frames for finding in _frame_findings(frame)
    )


def _frame_findings(data: str) -> tuple[WatchFinding, ...]:
    """The findings one decoded `data:` frame of the attention-feed sample carries.

    Classification reads the frame's own `event` field, the published
    contract every run-event resource carries -- never an SSE `event:`
    header, which this feed's routes never write. A frame this cannot even
    read as a JSON object is itself a finding: a feed the cockpit already
    trusts should never carry one.
    """

    try:
        carried = json.loads(data)
    except json.JSONDecodeError as error:
        return (
            WatchFinding(
                WatchFindingKind.STREAM_SAMPLE_UNREADABLE, EVENTS_PATH, str(error)
            ),
        )
    if not isinstance(carried, dict):
        return (
            WatchFinding(
                WatchFindingKind.STREAM_SAMPLE_UNREADABLE,
                EVENTS_PATH,
                "the event stream carried something that is not a JSON object",
            ),
        )
    kind = carried.get("event")
    if kind == STREAM_FAILURE_NAME:
        return _stream_failed_finding(data)
    if kind == RUN_PROJECTION_CORRUPT_NAME:
        return _run_projection_corrupt_finding(data)
    return ()


def _stream_failed_finding(data: str) -> tuple[WatchFinding, ...]:
    try:
        failure = StreamFailureResource.model_validate_json(data)
    except ValidationError as error:
        return (_unreadable_finding(EVENTS_PATH, error),)
    return (
        WatchFinding(
            WatchFindingKind.STREAM_FAILED,
            EVENTS_PATH,
            _problem_sentence(failure.problem),
        ),
    )


def _run_projection_corrupt_finding(data: str) -> tuple[WatchFinding, ...]:
    try:
        corrupt = RunProjectionCorruptResource.model_validate_json(data)
    except ValidationError as error:
        return (_unreadable_finding(EVENTS_PATH, error),)
    return (
        WatchFinding(
            WatchFindingKind.RUN_PROJECTION_CORRUPT,
            EVENTS_PATH,
            _problem_sentence(corrupt.problem),
            public_run_reference=corrupt.public_run_reference,
        ),
    )


def _problem_sentence(
    problem: ProblemResource | DurableStateCorruptProblemResource,
) -> str:
    return f"{problem.status} {problem.title}: {problem.detail}"


def _refusal_finding(
    endpoint: str, failure: AtelierApiTransportFailure
) -> WatchFinding:
    if failure.status is None:
        return WatchFinding(
            WatchFindingKind.SERVICE_UNREACHABLE, endpoint, failure.reason
        )
    try:
        problem = ProblemResource.model_validate_json(failure.body)
    except ValidationError:
        return WatchFinding(
            WatchFindingKind.RESPONSE_REFUSED,
            endpoint,
            f"{failure.status} {failure.reason}",
        )
    return WatchFinding(
        WatchFindingKind.RESPONSE_REFUSED, endpoint, _problem_sentence(problem)
    )


def _unreadable_finding(endpoint: str, error: ValidationError) -> WatchFinding:
    return WatchFinding(
        WatchFindingKind.RESPONSE_REFUSED,
        endpoint,
        f"the service answered something this cannot read: {error}",
    )


def add_watch_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Declare `watch`, the one word that reads a served instance and reports."""

    parser = commands.add_parser(
        "watch",
        help="read a served Atelier instance and report typed findings",
        description=WATCH_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE_URL,
        help=f"the served Atelier API to read (default {DEFAULT_SERVICE_URL})",
    )


def watch_exit_code(report: WatchReport) -> int:
    """0 when a `watch` call found nothing to report, else non-zero."""

    return 1 if report.findings else 0


def execute_watch(parsed: argparse.Namespace) -> int:
    try:
        report = watch_instance(parsed.service)
    except AtelierApiAddressUnusable as refusal:
        print(str(refusal), file=sys.stderr)
        return 2
    print(json.dumps(_report_document(report), indent=2))
    return watch_exit_code(report)


def _report_document(report: WatchReport) -> dict[str, object]:
    return {
        "service_url": report.service_url,
        "endpoints_read": list(report.endpoints_read),
        "findings": [
            {
                "kind": finding.kind,
                "endpoint": finding.endpoint,
                "detail": finding.detail,
                "public_run_reference": finding.public_run_reference,
            }
            for finding in report.findings
        ],
    }
