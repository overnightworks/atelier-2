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
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import ValidationError

from atelier2.api.problem_vocabulary import PROBLEM_DEFINITIONS
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
from atelier2.api.references import (
    InvalidPublicRunReference,
    decode_public_run_reference,
)
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
from atelier2.host.address import DEFAULT_SERVICE_URL
from atelier2.host.atelier_api_client import (
    AtelierApi,
    AtelierApiAddressUnusable,
    AtelierApiTransportFailure,
    BoundedEventSample,
    BoundedResponse,
    EventSampleLimit,
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
timeout and its whole enforced wall clock alike, since it is one bounded
read, never a stream this call keeps sampling."""

MAXIMUM_RESPONSE_BYTES: Final = 65_536
"""How much of any one fixed endpoint's answer this call ever buffers."""

EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS: Final = 2.0
"""The read timeout on every chunk of the attention-feed sample; a silent
feed stops the sample rather than the whole command."""

EVENT_SAMPLE_DEADLINE_SECONDS: Final = 3.0
"""The whole attention-feed sample's enforced wall clock."""

_TRANSPORT_LOGGER_NAMES: Final = ("httpx", "httpcore")
"""The libraries whose own request logging this command turns down at its
entry point: httpx logs every request's status line -- the far side's own
reason phrase included -- at `INFO`, and this command's whole contract is
that no text an answer wrote leaves it, through any channel."""

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

WITHHELD_RUN_REFERENCE: Final = "<run reference withheld>"
"""What a finding names instead of a run reference the API's own contract
does not recognize."""


class WatchFindingKind(StrEnum):
    """Every shape one `watch` call can report; absence of all of them is clean."""

    RESPONSE_REFUSED = "RESPONSE_REFUSED"
    SERVICE_UNREACHABLE = "SERVICE_UNREACHABLE"
    STREAM_FAILED = STREAM_FAILURE_NAME
    RUN_PROJECTION_CORRUPT = RUN_PROJECTION_CORRUPT_NAME
    STREAM_SAMPLE_UNREADABLE = "STREAM_SAMPLE_UNREADABLE"
    STREAM_NEVER_ANSWERED = "STREAM_NEVER_ANSWERED"
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
    """The wall-clock guarantee one endpoint's read gives, enforced.

    `deadline_seconds` is the whole read's absolute limit, held by the
    client's own alarm (`wall_clock_deadline`) rather than by a cooperative
    check a blocked read never reaches -- so it is the worst case, not a
    hope. `read_timeout_seconds` bounds one read within it, which is what
    tells a feed that went quiet from one that hung.
    """

    deadline_seconds: float
    read_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class AttentionFeedSample:
    """What one bounded look at the attention feed came back with.

    `stopped` is why the sample ended -- `frame-limit`, `byte-limit`,
    `overall-deadline`, `silent`, `closed-early`, `refused`, or
    `unreachable` -- named even on a clean report, since "this call saw
    nothing" and "this call saw a healthy feed" are not the same claim.
    `frames_read` and `bytes_read` say how much this call had actually seen
    by then: a sample the deadline cut short still reports the frames it
    read, findings included.
    """

    stopped: str
    frames_read: int
    bytes_read: int


@dataclass(frozen=True, slots=True)
class WatchReport:
    service_url: str
    endpoints_read: tuple[str, ...]
    findings: tuple[WatchFinding, ...]
    attention_feed_sample: AttentionFeedSample
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
            read_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            deadline_seconds=REQUEST_TIMEOUT_SECONDS,
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
    # `blocked_since` is safe to name: `RedeployBlockedResource` pins it to
    # `RECORDED_AT_PATTERN`, so a value that decoded at all cannot carry
    # anything but digits, dashes, `T`, colons, and `Z`. `.reason` is free
    # text the watcher wrote about its own failure and never appears here.
    since = health.redeploy.blocked_since or "an unrecorded time"
    return (
        WatchFinding(
            WatchFindingKind.REDEPLOY_BLOCKED, HEALTH_PATH, f"blocked since {since}"
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


def _event_sample(
    api: AtelierApi,
) -> tuple[tuple[WatchFinding, ...], AttentionFeedSample]:
    """The attention-feed sample's own findings, and what the sample saw.

    Hitting one of this call's own limits with something in hand -- a frame
    cap, a byte cap, a deadline that cut a live read short -- is a fact
    about the sample, not the feed: named in the report, never a finding.
    So is `silent`, a read that waited out its timeout, which is what an
    idle feed looks like. What is a finding is a feed that told this call
    nothing at all and cannot be explained by this call's own choices --
    see `_unanswered_feed_finding`.

    Whichever way it stopped, the frames already read are classified: a
    `STREAM_FAILED` frame this call did read stays in the report even when
    the deadline is what ended the read that followed it.
    """

    try:
        sample = api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS,
            deadline_seconds=EVENT_SAMPLE_DEADLINE_SECONDS,
            maximum_bytes=EVENT_SAMPLE_MAXIMUM_BYTES,
            maximum_frames=EVENT_SAMPLE_MAXIMUM_FRAMES,
        )
    except AtelierApiTransportFailure as failure:
        stopped = "unreachable" if failure.status is None else "refused"
        return (
            (_refusal_finding(EVENTS_PATH, failure),),
            AttentionFeedSample(stopped, frames_read=0, bytes_read=0),
        )
    findings = [
        finding for frame in sample.frames for finding in _frame_findings(frame)
    ]
    unanswered = _unanswered_feed_finding(sample)
    if unanswered is not None:
        findings.append(unanswered)
    stopped = "closed-early" if sample.stopped is None else sample.stopped.value
    return tuple(findings), AttentionFeedSample(
        stopped, frames_read=len(sample.frames), bytes_read=sample.bytes_read
    )


def _unanswered_feed_finding(sample: BoundedEventSample) -> WatchFinding | None:
    """Whether a sample that came back without a single frame is the feed's
    own doing rather than this call's budget.

    Two shapes are: the connection ended, though this feed is documented to
    never end on its own; and the whole deadline passing without one byte of
    body arriving, which is a feed that never spoke -- a reply whose headers
    trickle in forever looks exactly like this, and reporting it clean would
    be the watcher lying about an instance it could not read.
    """

    if sample.frames:
        return None
    if sample.stopped is None:
        silence = (
            "this read's connection closed before it ever sent a byte"
            if sample.bytes_read == 0
            else "this read's connection closed without ever completing a data frame"
        )
    elif sample.stopped is EventSampleLimit.OVERALL_DEADLINE and sample.bytes_read == 0:
        silence = "it sent no byte at all within this read's whole deadline"
    else:
        return None
    return WatchFinding(
        WatchFindingKind.STREAM_NEVER_ANSWERED,
        EVENTS_PATH,
        f"the attention feed is documented to never end on its own; {silence}",
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
            public_run_reference=_named_run(corrupt.public_run_reference),
        ),
    )


def _named_run(reference: str) -> str:
    """The run reference as this API's own contract encodes one, or a
    withheld marker.

    `decode_public_run_reference` is the contract's own parser: it accepts
    only a canonical `run1.` reference that re-encodes to exactly itself, so
    a value shaped like one but carrying an answer's own text -- which the
    field's pattern alone would still admit -- never reaches the report.
    """

    try:
        decode_public_run_reference(reference)
    except InvalidPublicRunReference:
        return WITHHELD_RUN_REFERENCE
    return reference


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


_KNOWN_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    name
    for model in (
        HealthResource,
        RedeployBlockedResource,
        SeatResource,
        ProblemResource,
        StreamFailureResource,
        RunProjectionCorruptResource,
        DurableStateCorruptProblemResource,
    )
    for name in model.model_fields
)
"""Every field name a decode this module runs could legitimately name in a
`ValidationError`'s `loc`. A pydantic `loc` is built from the document's own
keys once a model turns `extra="forbid"` on an unrecognized one, so an
unlisted name is not a field this module ever declared -- it is the
document's own text, and `_field_path` never repeats it."""


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
    if not location:
        return "<root>"
    return ".".join(_field_part(part) for part in location)


def _field_part(part: object) -> str:
    if isinstance(part, int):
        return str(part)
    if part in _KNOWN_FIELD_NAMES:
        return str(part)
    return "<unknown field>"


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


def execute_watch(parsed: argparse.Namespace, *, api: AtelierApi | None = None) -> int:
    """Run one `watch` and print its report; `api` is the same test seam
    `watch_instance` takes, so a test drives this whole entry point."""

    _quiet_transport_logging()
    try:
        report = watch_instance(parsed.service, api=api)
    except AtelierApiAddressUnusable as refusal:
        print(str(refusal), file=sys.stderr)
        return 2
    print(json.dumps(_report_document(report), indent=2))
    return watch_exit_code(report)


def _quiet_transport_logging() -> None:
    """Turn down the transport libraries' own request logging for this
    command's process -- see `_TRANSPORT_LOGGER_NAMES` for why this command
    owns that decision at all."""

    for name in _TRANSPORT_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.WARNING)


def _budget_document(budget: WatchBudget) -> dict[str, object]:
    return {
        "deadline_seconds": budget.deadline_seconds,
        "read_timeout_seconds": budget.read_timeout_seconds,
    }


def _report_document(report: WatchReport) -> dict[str, object]:
    sample = report.attention_feed_sample
    return {
        "service_url": report.service_url,
        "endpoints_read": list(report.endpoints_read),
        "attention_feed_sample": {
            "stopped": sample.stopped,
            "frames_read": sample.frames_read,
            "bytes_read": sample.bytes_read,
        },
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
