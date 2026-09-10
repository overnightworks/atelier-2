"""`atelier2 watch`: read a served instance's fixed doors and report what it saw.

This is a read-only observer, not a client of any one feature: it asks the
same doors an operator or the cockpit already asks -- health, seat, the run
and catalog listings, and a bounded sample of the attention feed -- and turns
what each answered into a typed finding. Nothing here starts, cancels, or
publishes anything, and nothing it reads ever reaches the report unredacted:
the seat's terminal address carries the terminal's own access token
(`served_seat.py`), so only the seat's state is ever named.

One call is two phases. The reading phase makes every network call this
command makes, all of them under one deadline (`reading_client`); when that
deadline falls, the phase ends where it stands and its client is never
touched again. The reporting phase is pure: it classifies what the reading
phase collected, and it opens no socket, reads nothing, and closes nothing.
A watch is one process; a deadline ends its reading, never its report.

Every call this command makes is one GET, on one path from `WATCH_ENDPOINTS`.
The attention feed is sampled, never followed, because it is documented to
never end on its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import httpx
from pydantic import ValidationError

from atelier2.api.problem_vocabulary import PROBLEM_DEFINITIONS
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
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
    EVENT_STREAM_MEDIA_TYPE,
    AtelierApi,
    AtelierApiAddressUnusable,
    AtelierApiTransportFailure,
    BoundedRead,
    EventFrameCollection,
    EventSampleOutcome,
    WallClockDeadlineExceeded,
    reading_client,
)
from atelier2.host.run_command import RUN_PATH, STREAM_FAILURE_NAME

WATCH_DESCRIPTION = """\
Read a served Atelier instance's fixed doors and report typed findings.

Offline in the sense migrate and connect are: this changes nothing. It asks
health, seat, the runs and workflow-revisions listings, and a bounded sample
of the attention feed, then prints one JSON report to stdout -- empty and
exit 0 when nothing was found, non-empty and a non-zero exit otherwise.
"""

WATCH_DEADLINE_SECONDS: Final = 25.0
"""The whole reading phase's enforced wall clock -- one alarm over every
network call together, wide enough that each door's own read timeout is what
normally ends it, and narrow enough that an instance which only trickles
bytes cannot hold the observer for longer than an operator would wait."""

REQUEST_TIMEOUT_SECONDS: Final = 5.0
"""How long one plain GET among the fixed doors may wait for its next chunk
before it counts as unreachable."""

MAXIMUM_RESPONSE_BYTES: Final = 65_536
"""How much of any one fixed door's answer this call ever buffers."""

EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS: Final = 2.0
"""The read timeout on every chunk of the attention-feed sample; a silent
feed stops the sample rather than the whole command."""

EVENT_SAMPLE_MAXIMUM_BYTES: Final = 65_536
EVENT_SAMPLE_MAXIMUM_FRAMES: Final = 20

TRANSPORT_LOGGER_NAMES: Final = ("httpx", "httpcore")
"""The libraries whose own sub-warning records this command drops on the
handlers it prints through: httpx logs every request's status line -- the far
side's own reason phrase included -- at `INFO`, and httpcore logs a reply's
headers at `DEBUG`."""

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
"""The fixed, complete list of paths one `watch` call reads, in reading order
-- one GET each, no retry, no pagination; a page beyond the first is each list
endpoint's own paging defect class, watched for elsewhere (#1313, #1501), not
here. The feed comes last because it is the one read that can spend the whole
deadline, and it must not starve the cheap doors before it."""

DOOR_ENDPOINTS: Final[tuple[str, ...]] = tuple(
    endpoint for endpoint in WATCH_ENDPOINTS if endpoint != EVENTS_PATH
)
"""The plain GETs among them: every path but the feed, which is sampled."""

RUN_PROJECTION_CORRUPT_NAME: Final[str] = RunProjectionCorruptResource.model_fields[
    "event"
].default

_WORKBENCH_SENTENCE: Final = (
    "open the Workbench: the run this frame names is marked there"
)
"""How a finding points at one run without printing its reference. The
reference is a free string the API's own field pattern would let an answer
dress up as one, and a watcher that prints it would carry that answer's text
out; the class of defect plus where to look is the whole diagnosis."""


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
    READING_CUT_SHORT = "READING_CUT_SHORT"


@dataclass(frozen=True, slots=True)
class WatchFinding:
    """One typed observation, named the same way whichever endpoint raised it."""

    kind: WatchFindingKind
    endpoint: str
    detail: str


@dataclass(frozen=True, slots=True)
class WatchBudget:
    """The wall-clock guarantee one `watch` call gives, enforced.

    `deadline_seconds` is the whole reading phase's absolute limit, held by
    the process's own alarm (`wall_clock_deadline`) rather than by a
    cooperative check a blocked read never reaches -- so it is the worst
    case, not a hope. The two read timeouts bound one read within it, which
    is what tells a feed that went quiet from one that hangs.
    """

    deadline_seconds: float
    door_read_timeout_seconds: float
    event_sample_read_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class AttentionFeedSample:
    """What one bounded look at the attention feed came back with.

    `outcome` is named even on a clean report, since "this call saw nothing"
    and "this call saw a healthy feed" are not the same claim.
    `frames_read` and `bytes_read` say how much this call had actually seen
    by then: a sample the deadline cut short still reports the frames it read,
    findings included.
    """

    outcome: EventSampleOutcome
    frames_read: int
    bytes_read: int


@dataclass(slots=True)
class DoorReading:
    """What one fixed door answered, as far as the reading phase got.

    Registered before its own read starts, so a door the deadline interrupted
    is in the report as a door that was being read, not as one never asked.
    """

    endpoint: str
    answer: BoundedRead = field(default_factory=BoundedRead)
    refusal: AtelierApiTransportFailure | None = None
    answered: bool = False


@dataclass(slots=True)
class InstanceReading:
    """Everything the reading phase gathered, whatever ended it.

    Every field is written while the phase runs and never afterwards: the
    deadline can end the phase inside any read, and the report is built from
    exactly what this holds at that moment.
    """

    doors: list[DoorReading] = field(default_factory=list)
    feed: EventFrameCollection = field(default_factory=EventFrameCollection)
    feed_refusal: AtelierApiTransportFailure | None = None
    deadline_passed: bool = False


@dataclass(frozen=True, slots=True)
class WatchReport:
    service_url: str
    endpoints_read: tuple[str, ...]
    findings: tuple[WatchFinding, ...]
    attention_feed_sample: AttentionFeedSample
    budget: WatchBudget


def read_instance(
    service_url: str, *, transport: httpx.BaseTransport | None = None
) -> InstanceReading:
    """The reading phase: every network call this command makes, under one
    deadline, into one collection.

    `transport` is a test seam only, handed straight to the one client this
    phase owns; production composition leaves it unset, so it reaches the real
    network. When the deadline falls, this returns what it has -- the client
    stays where it was interrupted and is never used again.
    """

    reading = InstanceReading()
    try:
        with reading_client(
            service_url, WATCH_DEADLINE_SECONDS, transport=transport
        ) as api:
            for endpoint in DOOR_ENDPOINTS:
                _read_door(api, endpoint, reading)
            _read_feed(api, reading)
    except WallClockDeadlineExceeded:
        reading.deadline_passed = True
    return reading


def _read_door(api: AtelierApi, endpoint: str, reading: InstanceReading) -> None:
    door = DoorReading(endpoint)
    reading.doors.append(door)
    try:
        api.bounded_get(
            endpoint,
            read_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            maximum_bytes=MAXIMUM_RESPONSE_BYTES,
            into=door.answer,
        )
    except AtelierApiTransportFailure as failure:
        door.refusal = failure
    door.answered = True


def _read_feed(api: AtelierApi, reading: InstanceReading) -> None:
    try:
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS,
            maximum_bytes=EVENT_SAMPLE_MAXIMUM_BYTES,
            maximum_frames=EVENT_SAMPLE_MAXIMUM_FRAMES,
            into=reading.feed,
        )
    except AtelierApiTransportFailure as failure:
        reading.feed_refusal = failure
        reading.feed.outcome = _feed_outcome_of(failure, reading.feed)


def _feed_outcome_of(
    failure: AtelierApiTransportFailure, feed: EventFrameCollection
) -> EventSampleOutcome:
    """A service that answered nothing at all is unreachable; one that sent
    bytes or a status and was then refused by this client is not."""

    if failure.status is not None or feed.bytes_read > 0:
        return EventSampleOutcome.REFUSED
    return EventSampleOutcome.UNREACHABLE


def watch_report(service_url: str, reading: InstanceReading) -> WatchReport:
    """The reporting phase: classify what the reading phase collected.

    Pure by contract -- no request, no close, nothing that could touch a
    client the deadline abandoned. Classifying the feed's frames here rather
    than as they arrive is what makes a `STREAM_FAILED` frame survive a
    deadline that ended the read right after it.
    """

    findings = [finding for door in reading.doors for finding in _door_findings(door)]
    findings.extend(_feed_findings(reading))
    if reading.deadline_passed:
        findings.append(_deadline_finding(reading))
    return WatchReport(
        service_url=service_url,
        endpoints_read=_endpoints_read(reading),
        findings=tuple(findings),
        attention_feed_sample=AttentionFeedSample(
            outcome=reading.feed.outcome,
            frames_read=len(reading.feed.frames),
            bytes_read=reading.feed.bytes_read,
        ),
        budget=WatchBudget(
            deadline_seconds=WATCH_DEADLINE_SECONDS,
            door_read_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            event_sample_read_timeout_seconds=EVENT_SAMPLE_REQUEST_TIMEOUT_SECONDS,
        ),
    )


def watch_instance(
    service_url: str, *, transport: httpx.BaseTransport | None = None
) -> WatchReport:
    """Read a served instance once, then report what the reading saw."""

    return watch_report(service_url, read_instance(service_url, transport=transport))


def _endpoints_read(reading: InstanceReading) -> tuple[str, ...]:
    reached = [door.endpoint for door in reading.doors]
    if reading.feed.outcome is not EventSampleOutcome.UNREAD:
        reached.append(EVENTS_PATH)
    return tuple(reached)


def _deadline_finding(reading: InstanceReading) -> WatchFinding:
    """Where the reading phase stood when its deadline fell.

    A report that ended early is never a clean report: what this command did
    not get to read, it cannot call healthy.
    """

    unanswered = [door.endpoint for door in reading.doors if not door.answered]
    endpoint = unanswered[0] if unanswered else EVENTS_PATH
    return WatchFinding(
        WatchFindingKind.READING_CUT_SHORT,
        endpoint,
        f"this read's whole deadline of {WATCH_DEADLINE_SECONDS} seconds "
        f"passed while the instance was still answering",
    )


def _door_findings(door: DoorReading) -> tuple[WatchFinding, ...]:
    """What one door's answer says, whichever status carried it.

    A problem document is a finding wherever it appears: a 2xx body that is
    secretly one of this API's own problem documents is exactly as real as a
    non-2xx one, so both are recognized before the door's own published shape
    ever sees a body at all. A door still answering when the reading phase
    ended has nothing to say yet -- the deadline finding is what names it.
    """

    if not door.answered:
        return ()
    if door.refusal is not None:
        return (_refusal_finding(door.endpoint, door.refusal),)
    body = bytes(door.answer.body)
    problem = _problem_finding(door.endpoint, body)
    if problem is not None:
        return (problem,)
    if door.answer.status is None or not 200 <= door.answer.status < 300:
        return (_status_finding(door),)
    return _DOOR_SHAPES[door.endpoint](body)


def _status_finding(door: DoorReading) -> WatchFinding:
    return WatchFinding(
        WatchFindingKind.RESPONSE_REFUSED,
        door.endpoint,
        f"answered {door.answer.status} without one of this API's own "
        f"problem documents",
    )


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


def _listing_decoded(body: bytes) -> tuple[WatchFinding, ...]:
    """A listing that answered 2xx and carries no problem document says all
    this observer asks of it; whether its pages are complete is the paging
    defect class watched for elsewhere."""

    del body
    return ()


_DOOR_SHAPES: Final[dict[str, Callable[[bytes], tuple[WatchFinding, ...]]]] = {
    HEALTH_PATH: _health_decoded,
    SEAT_PATH: _seat_decoded,
    RUN_PATH: _listing_decoded,
    WORKFLOW_REVISIONS_PATH: _listing_decoded,
}
"""Each door's own published shape, read only after a body proved to be no
problem document."""


def _feed_findings(reading: InstanceReading) -> tuple[WatchFinding, ...]:
    """The attention-feed sample's findings, from whatever it collected.

    Hitting one of this call's own limits with something in hand -- a frame
    cap, a byte cap -- is a fact about the sample, not the feed: named in the
    report, never a finding. So is `silent` with nothing read, which is what
    an idle feed looks like. How the sample ended is one finding at most: a
    refusal when this client refused the reply, otherwise whether the feed
    told this call anything usable -- see `_unanswered_feed_finding`.
    """

    findings = [
        finding for frame in reading.feed.frames for finding in _frame_findings(frame)
    ]
    ending = (
        _refusal_finding(EVENTS_PATH, reading.feed_refusal)
        if reading.feed_refusal is not None
        else _unanswered_feed_finding(reading.feed)
    )
    if ending is not None:
        findings.append(ending)
    return tuple(findings)


def _unanswered_feed_finding(feed: EventFrameCollection) -> WatchFinding | None:
    """Whether a sample that came back without a single frame is the feed's
    own doing rather than this call's budget.

    Bytes without a frame is one: whatever those bytes were -- a heartbeat
    comment, half a frame, an error page this client already refused -- the
    feed did not answer as a feed within the whole sample, and reporting that
    clean would be the watcher lying about an instance it could not read. So
    is a connection that ended, though this feed is documented never to end on
    its own, and so is a whole reading phase passing without one byte of body.
    A sample that never ran is not: the deadline finding already names it.
    """

    if feed.frames or feed.outcome is EventSampleOutcome.UNREAD:
        return None
    if feed.bytes_read > 0:
        silence = "it sent bytes that never completed one data frame"
    elif feed.outcome is EventSampleOutcome.CLOSED_EARLY:
        silence = "this read's connection closed before it ever sent a byte"
    elif feed.outcome is EventSampleOutcome.INTERRUPTED:
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
            f"{_problem_sentence(corrupt.problem)}; {_WORKBENCH_SENTENCE}",
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


def execute_watch(
    parsed: argparse.Namespace, *, transport: httpx.BaseTransport | None = None
) -> int:
    """Run one `watch` and print its report.

    Reading first, reporting after: the report is printed from what the
    reading phase collected, whether that phase finished or its deadline
    ended it. `transport` is the same test seam `read_instance` takes.
    """

    silence_transport_chatter()
    try:
        report = watch_instance(parsed.service, transport=transport)
    except AtelierApiAddressUnusable as refusal:
        print(str(refusal), file=sys.stderr)
        return 2
    print(json.dumps(_report_document(report), indent=2))
    return watch_exit_code(report)


class TransportChatterFilter(logging.Filter):
    """Drops a transport library's own sub-warning records, whichever logger
    of its family made them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not any(
            record.name == family or record.name.startswith(f"{family}.")
            for family in TRANSPORT_LOGGER_NAMES
        )


def silence_transport_chatter() -> None:
    """Drop the transport libraries' chatter on the handlers this process
    prints through.

    A level set on the `httpx` or `httpcore` logger does not hold: a record
    made on `httpcore.http11` is filtered by that child's own level alone and
    then walks straight past its ancestors' levels to their handlers. The
    handler is therefore the only place that can decide for a whole family,
    and `watch` owns the process it runs in: its own report goes to stdout,
    and the handlers below carry whatever else this process says.
    """

    chatter = TransportChatterFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(chatter)
    for name in TRANSPORT_LOGGER_NAMES:
        # Delivery for the whole family goes through the handlers just
        # filtered: a handler these loggers carry themselves would be reached
        # before those and would never see the filter.
        family = logging.getLogger(name)
        family.handlers.clear()
        family.propagate = True


def _report_document(report: WatchReport) -> dict[str, object]:
    sample = report.attention_feed_sample
    return {
        "service_url": report.service_url,
        "endpoints_read": list(report.endpoints_read),
        "attention_feed_sample": {
            "outcome": sample.outcome.value,
            "frames_read": sample.frames_read,
            "bytes_read": sample.bytes_read,
        },
        "budget": {
            "deadline_seconds": report.budget.deadline_seconds,
            "door_read_timeout_seconds": report.budget.door_read_timeout_seconds,
            "event_sample_read_timeout_seconds": (
                report.budget.event_sample_read_timeout_seconds
            ),
        },
        "findings": [
            {
                "kind": finding.kind.value,
                "endpoint": finding.endpoint,
                "detail": finding.detail,
            }
            for finding in report.findings
        ],
    }
