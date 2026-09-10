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
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import ValidationError

from atelier2.api.problem_vocabulary import PROBLEM_DEFINITIONS
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
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
    BoundedResponse,
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
"""How long one plain GET among the fixed endpoints may take -- its read
timeout and its whole wall-clock budget alike, since it is one bounded read,
never a stream this call keeps sampling."""

MAXIMUM_RESPONSE_BYTES: Final = 65_536
"""How much of any one fixed endpoint's answer this call ever buffers."""

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
    STREAM_CLOSED_EARLY = "STREAM_CLOSED_EARLY"
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
class WatchBudget:
    """The wall-clock guarantee this call's own timing honestly gives.

    `deadline_seconds` is checked before every read a call makes, but not
    inside one already under way: assembling one response's headers, or one
    body chunk, still carries its own `read_timeout_seconds` on top. A read
    already in flight when the deadline passes still finishes or times out
    on its own terms, so the true worst case for one endpoint is its
    deadline plus one read timeout, never the deadline alone.
    """

    deadline_seconds: float
    read_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class WatchReport:
    service_url: str
    endpoints_read: tuple[str, ...]
    findings: tuple[WatchFinding, ...]
    attention_feed_sample: str
    """Why the attention-feed sample itself stopped -- `frame-limit`,
    `byte-limit`, `overall-deadline`, `silent`, `refused`, or `closed-early`
    -- named here even on a clean report, since "this call saw nothing" and
    "this call saw a healthy feed" are not the same claim."""
    endpoint_budget: WatchBudget
    event_sample_budget: WatchBudget


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
    event_findings, attention_feed_sample = _event_sample(api)
    findings: list[WatchFinding] = [
        *_health_findings(api),
        *_seat_findings(api),
        *_listing_findings(api, RUN_PATH),
        *_listing_findings(api, WORKFLOW_REVISIONS_PATH),
        *event_findings,
    ]
    return WatchReport(
        service_url=service_url,
        endpoints_read=WATCH_ENDPOINTS,
        findings=tuple(findings),
        attention_feed_sample=attention_feed_sample,
        endpoint_budget=WatchBudget(
            deadline_seconds=REQUEST_TIMEOUT_SECONDS,
            read_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        ),
        event_sample_budget=WatchBudget(
            deadline_seconds=EVENT_SAMPLE_DEADLINE_SECONDS,
            read_timeout_seconds=EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS,
        ),
    )


def _endpoint_findings(
    api: AtelierApi,
    endpoint: str,
    decode: Callable[[bytes], tuple[WatchFinding, ...]],
) -> tuple[WatchFinding, ...]:
    """Read one fixed endpoint on a budget and classify what it answered.

    A problem document is a finding wherever it appears, independent of the
    status that carried it: a 2xx body that is secretly one of this API's own
    problem documents is exactly as real as a non-2xx one, so both are
    recognized before `decode` -- the endpoint's own published shape -- ever
    sees a body at all.
    """

    try:
        answered = api.bounded_get(
            endpoint,
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            overall_deadline_seconds=REQUEST_TIMEOUT_SECONDS,
            maximum_bytes=MAXIMUM_RESPONSE_BYTES,
        )
    except AtelierApiTransportFailure as failure:
        return (_refusal_finding(endpoint, failure),)
    problem = _problem_finding(endpoint, answered.body)
    if problem is not None:
        return (problem,)
    if not 200 <= answered.status < 300:
        return (_status_finding(endpoint, answered),)
    return decode(answered.body)


def _status_finding(endpoint: str, answered: BoundedResponse) -> WatchFinding:
    return WatchFinding(
        WatchFindingKind.RESPONSE_REFUSED,
        endpoint,
        f"answered {answered.status} without one of this API's own problem documents",
    )


def _health_findings(api: AtelierApi) -> tuple[WatchFinding, ...]:
    return _endpoint_findings(api, HEALTH_PATH, _health_decoded)


def _health_decoded(body: bytes) -> tuple[WatchFinding, ...]:
    try:
        health = HealthResource.model_validate_json(body)
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
    return _endpoint_findings(api, SEAT_PATH, _seat_decoded)


def _seat_decoded(body: bytes) -> tuple[WatchFinding, ...]:
    try:
        seat = SeatResource.model_validate_json(body)
    except ValidationError as error:
        return (_unreadable_finding(SEAT_PATH, error),)
    if seat.state is SeatState.ALIVE:
        return ()
    # Never the address `seat.url` may carry: that would hand the terminal's
    # own access token to anything reading this report.
    return (WatchFinding(WatchFindingKind.SEAT_NOT_ALIVE, SEAT_PATH, seat.state.value),)


def _listing_findings(api: AtelierApi, endpoint: str) -> tuple[WatchFinding, ...]:
    return _endpoint_findings(api, endpoint, lambda _body: ())


def _event_sample(api: AtelierApi) -> tuple[tuple[WatchFinding, ...], str]:
    """The attention-feed sample's own findings, and why the sample stopped.

    A feed the cockpit already trusts to never end on its own can stop this
    call three honest ways: it hit one of this call's own limits (frame,
    byte, or deadline) -- a fact about the sample, not the feed, so it is
    named in the report but is never itself a finding; it went quiet within
    a read's own timeout, `silent` -- also expected of an idle feed, so it
    too is named but not raised; or the connection itself ended before any
    of those did. That last one is the one case this call cannot explain as
    its own choice, so it alone is a finding -- worded by what actually
    arrived: `bytes_read == 0` is a connection that answered nothing at all,
    never mind a data frame; a positive `bytes_read` with no frame is a
    connection that spoke (a heartbeat comment, most likely) and still
    closed without ever completing one. A close after a real frame already
    arrived is still named as `closed-early` in the report -- an operator
    reading it is not left guessing -- without being raised as a finding on
    its own.
    """

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
        outcome = "unreachable" if failure.status is None else "refused"
        return (_refusal_finding(EVENTS_PATH, failure),), outcome
    findings = [
        finding for frame in sample.frames for finding in _frame_findings(frame)
    ]
    if sample.stopped is not None:
        return tuple(findings), sample.stopped.value
    if not sample.frames:
        findings.append(
            WatchFinding(
                WatchFindingKind.STREAM_CLOSED_EARLY,
                EVENTS_PATH,
                "the attention feed is documented to never end on its own; "
                + (
                    "this read's connection closed before it ever sent a byte"
                    if sample.bytes_read == 0
                    else "this read's connection closed without ever "
                    "completing a data frame"
                ),
            )
        )
    return tuple(findings), "closed-early"


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
    problem = _problem_finding(EVENTS_PATH, data.encode())
    return () if problem is None else (problem,)


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
    """A sentence built only from this repository's own problem vocabulary --
    never `problem.title` or `.type` as the answer spelled them. Both are
    free strings a served problem document does not have to keep honest
    (unlike `status`, a strictly typed int): a document naming a type this
    vocabulary does not recognize could otherwise ride any text at all back
    out through `.title`, or through an unmatched tail of `.type` itself, so
    an unrecognized type says only that -- nothing the answer wrote.
    """

    code = problem.type.removeprefix(PROBLEM_TYPE_PREFIX)
    definition = PROBLEM_DEFINITIONS.get(code)
    if definition is None:
        return f"{problem.status} unknown problem type"
    return f"{problem.status} {definition.title} [{PROBLEM_TYPE_PREFIX}{code}]"


def _problem_finding(endpoint: str, body: bytes) -> WatchFinding | None:
    """Whether `body` is one of this API's own problem documents -- checked
    on every body this reads, independent of whatever status carried it."""

    try:
        problem = ProblemResource.model_validate_json(body)
    except ValidationError:
        return None
    if not problem.type.startswith(PROBLEM_TYPE_PREFIX):
        return None
    return WatchFinding(
        WatchFindingKind.RESPONSE_REFUSED, endpoint, _problem_sentence(problem)
    )


def _refusal_finding(
    endpoint: str, failure: AtelierApiTransportFailure
) -> WatchFinding:
    if failure.status is None:
        return WatchFinding(
            WatchFindingKind.SERVICE_UNREACHABLE, endpoint, failure.reason
        )
    problem = _problem_finding(endpoint, failure.body)
    if problem is not None:
        return problem
    return WatchFinding(
        WatchFindingKind.RESPONSE_REFUSED,
        endpoint,
        f"{failure.status} {failure.reason}",
    )


def _unreadable_finding(endpoint: str, error: ValidationError) -> WatchFinding:
    """A diagnosis of *why* a body did not read as its published contract --
    the field path and the error kind, never a value: pydantic's own
    `ValidationError` embeds the offending input (and, for a `missing` field,
    every sibling value the document carried) in both its message and its
    `errors()` entries, which would hand a document's own secret back out
    through this report.
    """

    issues = error.errors()
    fields = ", ".join(_field_path(issue["loc"]) for issue in issues)
    kinds = ", ".join(sorted({str(issue["type"]) for issue in issues}))
    return WatchFinding(
        WatchFindingKind.RESPONSE_REFUSED,
        endpoint,
        f"the service answered something that does not read as its "
        f"published contract: field(s) {fields} ({kinds})",
    )


def _field_path(location: tuple[object, ...]) -> str:
    return ".".join(str(part) for part in location) if location else "<root>"


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


def _budget_document(budget: WatchBudget) -> dict[str, object]:
    return {
        "deadline_seconds": budget.deadline_seconds,
        "read_timeout_seconds": budget.read_timeout_seconds,
        "worst_case_seconds": budget.deadline_seconds + budget.read_timeout_seconds,
    }


def _report_document(report: WatchReport) -> dict[str, object]:
    return {
        "service_url": report.service_url,
        "endpoints_read": list(report.endpoints_read),
        "attention_feed_sample": report.attention_feed_sample,
        "endpoint_budget": _budget_document(report.endpoint_budget),
        "event_sample_budget": _budget_document(report.event_sample_budget),
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
