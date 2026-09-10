"""`atelier2 watch`: read a served instance's fixed doors and report what it saw.

This is a read-only observer, not a client of any one feature: it asks the
same doors an operator or the cockpit already asks -- health, seat, the run
and catalog listings, and a bounded sample of the attention feed -- and turns
what each answered into a typed finding. Nothing here starts, cancels, or
publishes anything, and nothing it reads ever reaches the report unredacted:
the seat's terminal address carries the terminal's own access token
(`served_seat.py`), so only the seat's state is ever named.

One call is two halves in two processes. The reading makes every network call
this command makes, in a child process under one deadline (`instance_reader`),
and streams what it saw back record by record. This process only reports: it
classifies the records that arrived, and it opens no socket, reads nothing,
and closes nothing. A deadline ends the reading, never the report -- what
arrived is reported, and that the reading was cut short is itself a finding.

Every call this command makes is one GET, on one path from `WATCH_ENDPOINTS`.
The attention feed is sampled, never followed, because it is documented to
never end on its own.
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
    RedeployBlockedResource,
    RunProjectionCorruptResource,
    SeatResource,
    StreamFailureResource,
)
from atelier2.host.address import DEFAULT_SERVICE_URL
from atelier2.host.atelier_api_client import (
    AtelierApiAddressUnusable,
    EventSampleOutcome,
)
from atelier2.host.instance_reader import (
    EVENTS_PATH,
    HEALTH_PATH,
    RUN_PATH,
    SEAT_PATH,
    WATCH_BUDGET,
    WATCH_ENDPOINTS,
    WORKFLOW_REVISIONS_PATH,
    DoorRead,
    DoorRefused,
    FeedReading,
    InstanceReading,
    ReaderFailure,
    ReadingBudget,
    ReadRefusal,
    read_instance,
)
from atelier2.host.run_command import STREAM_FAILURE_NAME

WATCH_DESCRIPTION = """\
Read a served Atelier instance's fixed doors and report typed findings.

Offline in the sense migrate and connect are: this changes nothing. It asks
health, seat, the runs and workflow-revisions listings, and a bounded sample
of the attention feed, then prints one JSON report to stdout -- empty and
exit 0 when nothing was found, non-empty and a non-zero exit otherwise.
"""

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
    READER_DIED = "READER_DIED"
    READER_FAILED = "READER_FAILED"


@dataclass(frozen=True, slots=True)
class WatchFinding:
    """One typed observation, named the same way whichever endpoint raised it."""

    kind: WatchFindingKind
    endpoint: str
    detail: str


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


@dataclass(frozen=True, slots=True)
class WatchReport:
    service_url: str
    endpoints_read: tuple[str, ...]
    findings: tuple[WatchFinding, ...]
    attention_feed_sample: AttentionFeedSample
    budget: ReadingBudget


def watch_report(
    service_url: str, reading: InstanceReading, budget: ReadingBudget
) -> WatchReport:
    """Classify what one reading delivered.

    Pure by contract -- no request, no process, nothing that could touch the
    reading that produced these records. Classifying the feed's frames here
    rather than as they arrive is what makes a `STREAM_FAILED` frame survive a
    deadline that ended the reading right after it.
    """

    findings = [finding for door in reading.doors for finding in _door_findings(door)]
    findings.extend(_feed_findings(reading.feed))
    findings.extend(_reading_findings(reading, budget))
    return WatchReport(
        service_url=service_url,
        endpoints_read=_endpoints_read(reading),
        findings=tuple(findings),
        attention_feed_sample=AttentionFeedSample(
            outcome=reading.feed.outcome,
            frames_read=len(reading.feed.frames),
            bytes_read=reading.feed.bytes_read,
        ),
        budget=budget,
    )


def watch_instance(service_url: str, budget: ReadingBudget) -> WatchReport:
    """Read a served instance once, then report what the reading saw."""

    return watch_report(service_url, read_instance(service_url, budget), budget)


def _endpoints_read(reading: InstanceReading) -> tuple[str, ...]:
    reached = [door.endpoint for door in reading.doors]
    if reading.feed.outcome is not EventSampleOutcome.UNREAD:
        reached.append(EVENTS_PATH)
    return tuple(reached)


def _unread_endpoint(reading: InstanceReading) -> str:
    """Where a reading that did not finish stood: the first path it has no
    answer for, in the order they are read."""

    reached = set(_endpoints_read(reading))
    return next(
        (endpoint for endpoint in WATCH_ENDPOINTS if endpoint not in reached),
        EVENTS_PATH,
    )


def _reading_findings(
    reading: InstanceReading, budget: ReadingBudget
) -> tuple[WatchFinding, ...]:
    """What the reading itself has to answer for.

    A report built from a reading that ended early is never a clean report:
    what this command did not get to read, it cannot call healthy. That is
    true of a deadline, of a reading that broke, and of a reading process that
    died -- silence from a dead reader must never pass for an instance with
    nothing to report. The reading's own account of what went wrong comes
    first, because it is the one that explains the rest; how it ended is read
    against what it had already said.
    """

    findings: list[WatchFinding] = []
    if reading.failure is not None:
        findings.append(_reader_failed_finding(reading, reading.failure))
    findings.extend(_ending_findings(reading, budget))
    return tuple(findings)


def _ending_findings(
    reading: InstanceReading, budget: ReadingBudget
) -> tuple[WatchFinding, ...]:
    """What the way the reading ended has to answer for.

    A reading that said its last word is complete, and nothing its process
    then does on the way out takes that back: one slow to leave is stopped by
    this read itself, and the code that stop produces says how loaded the
    observer's own machine was, not what the instance answered.

    What an ending can add is therefore only ever about a reading that never
    got to say it: one whose process would not be killed, one the deadline
    interrupted, and one that is simply gone -- including one that named its
    trouble and died anyway, because what ended that process is not what it
    reported. A reading that named its trouble and then left cleanly is a
    reader that broke, and that failure is the whole finding.
    """

    if reading.reader_ended:
        return ()
    if not reading.reader_reaped:
        return (_reader_died_finding(reading, _UNKILLABLE_SENTENCE),)
    if reading.deadline_passed:
        return (
            WatchFinding(
                WatchFindingKind.READING_CUT_SHORT,
                _unread_endpoint(reading),
                f"{_CUT_SHORT_SENTENCE} of {budget.deadline_seconds} seconds",
            ),
        )
    if reading.failure is not None and reading.reader_exit_code == _CLEAN_EXIT_CODE:
        return ()
    return (
        _reader_died_finding(reading, f"it ended with code {reading.reader_exit_code}"),
    )


def _reader_died_finding(reading: InstanceReading, why: str) -> WatchFinding:
    """Where the reading stood is the finding's endpoint; how its process
    ended is all the detail says, since a reading that ended cleanly and one
    that read every door are not the same claim."""

    return WatchFinding(
        WatchFindingKind.READER_DIED,
        _unread_endpoint(reading),
        f"the process reading this instance: {why}",
    )


def _reader_failed_finding(
    reading: InstanceReading, failure: ReaderFailure
) -> WatchFinding:
    """What the reading said went wrong in itself -- a phase and a category,
    never a message: an unexpected exception's text is the one place a library
    or the far side could still write into this report."""

    return WatchFinding(
        WatchFindingKind.READER_FAILED,
        _unread_endpoint(reading),
        f"the process reading this instance ran into trouble it did not "
        f"expect while {failure.phase.value}: {failure.category.value}",
    )


_CLEAN_EXIT_CODE: Final = 0
"""What the reading process comes back with when it left on its own; in a
reading that never said its last word, anything else -- a signal's negative
code included -- is a death."""

_UNKILLABLE_SENTENCE: Final = (
    "it ignored being stopped and being killed and was still running when "
    "this report was built"
)

_CUT_SHORT_SENTENCE: Final = "the reading did not complete within its whole budget"
"""What a deadline that fell says, and all it says: which side ran out the
clock is not something this end knows -- an instance still answering, a
sample that could not be finished, and a reading whose own last records were
still on their way all look the same from here. What was read stands beside
it in `endpoints_read` and the sample's counters."""


def _door_findings(door: DoorRead | DoorRefused) -> tuple[WatchFinding, ...]:
    """What one door's answer says, whichever status carried it.

    A problem document is a finding wherever it appears: a 2xx body that is
    secretly one of this API's own problem documents is exactly as real as a
    non-2xx one, so both are recognized before the door's own published shape
    ever sees a body at all.
    """

    if isinstance(door, DoorRefused):
        return (_refusal_finding(door.endpoint, door.refusal),)
    problem = _problem_finding(door.endpoint, door.answer.body)
    if problem is not None:
        return (problem,)
    if not 200 <= door.answer.status < 300:
        return (
            WatchFinding(
                WatchFindingKind.RESPONSE_REFUSED,
                door.endpoint,
                f"answered {door.answer.status} without one of this API's own "
                f"problem documents",
            ),
        )
    return _DOOR_SHAPES[door.endpoint](door.answer.body)


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


def _feed_findings(feed: FeedReading) -> tuple[WatchFinding, ...]:
    """The attention-feed sample's findings, from whatever it collected.

    Hitting one of the reading's own limits with something in hand -- a frame
    cap, a byte cap -- is a fact about the sample, not the feed: named in the
    report, never a finding. So is `silent` with nothing read, which is what
    an idle feed looks like. How the sample ended is one finding at most: a
    refusal when the client refused the reply, otherwise whether the feed
    told the reading anything usable -- see `_unanswered_feed_finding`.
    """

    findings = [finding for frame in feed.frames for finding in _frame_findings(frame)]
    ending = (
        _refusal_finding(EVENTS_PATH, feed.refusal)
        if feed.refusal is not None
        else _unanswered_feed_finding(feed)
    )
    if ending is not None:
        findings.append(ending)
    return tuple(findings)


def _unanswered_feed_finding(feed: FeedReading) -> WatchFinding | None:
    """Whether a sample that came back without a single frame is the feed's
    own doing rather than the reading's budget.

    Bytes without a frame is one: whatever those bytes were -- a heartbeat
    comment, half a frame, an error page this client already refused -- the
    feed did not answer as a feed within the whole sample, and reporting that
    clean would be the watcher lying about an instance it could not read. So
    is a connection that ended, though this feed is documented never to end on
    its own, and so is a whole reading passing without one byte of body. A
    sample that never ran is not: the reading's own ending names it.
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


def _refusal_finding(endpoint: str, refusal: ReadRefusal) -> WatchFinding:
    if refusal.status is None:
        return WatchFinding(
            WatchFindingKind.SERVICE_UNREACHABLE, endpoint, refusal.reason
        )
    problem = _problem_finding(endpoint, refusal.body)
    if problem is not None:
        return problem
    return WatchFinding(
        WatchFindingKind.RESPONSE_REFUSED,
        endpoint,
        f"{refusal.status} {refusal.reason}",
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


def execute_watch(parsed: argparse.Namespace) -> int:
    """Run one `watch` and print its report.

    Reading first, reporting after: the report is printed from what the
    reading delivered, whether that reading finished or its deadline ended it.
    """

    try:
        report = watch_instance(parsed.service, WATCH_BUDGET)
    except AtelierApiAddressUnusable as refusal:
        print(str(refusal), file=sys.stderr)
        return 2
    print(json.dumps(_report_document(report), indent=2))
    return watch_exit_code(report)


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
