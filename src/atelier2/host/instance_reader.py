"""The reading half of `atelier2 watch`: a child process reads a served
instance and streams what it saw, record by record.

Why a process rather than a timer. Every call this reading makes goes through
httpx, and a deadline that has to interrupt a blocking read from inside the
process can only do it by throwing into somebody else's code: an exception
landing in a connection pool's own critical section leaves that lock held, and
the observer deadlocks before it can report. Here the reading cannot hang the
observer at all. The parent holds one end of a pipe and a clock; the child
holds the client, the sockets, and every lock they need; and a child still
reading when the deadline falls is terminated, killed if it must be, and
reclaimed by the operating system with everything it held.

Nothing but records crosses the pipe: typed, picklable, and carrying no
exception object, because a library's exception carries its own message and
the far side's text with it. A refusal becomes this repository's own fixed
diagnosis (`AtelierApiTransportFailure.reason`) before it is ever sent. Each
record leaves the moment it is known, so a reading the deadline ends has
already delivered every door and every frame it read.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from typing import Final, assert_never

from atelier2.host.atelier_api_client import (
    EVENT_STREAM_MEDIA_TYPE,
    AtelierApi,
    AtelierApiTransportFailure,
    BoundedRead,
    EventSampleOutcome,
    EventSampleTally,
    api_base_url,
)
from atelier2.host.run_command import RUN_PATH

HEALTH_PATH: Final = "/health"
SEAT_PATH: Final = "/seat"
WORKFLOW_REVISIONS_PATH: Final = "/workflow-revisions"
EVENTS_PATH: Final = "/events"

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

READING_DEADLINE_SECONDS: Final = 25.0
"""The whole reading's wall clock, measured from before the child is even
started -- wide enough that each door's own read timeout is what normally ends
it, and narrow enough that an instance which only trickles bytes cannot hold
the observer for longer than an operator would wait."""

DOOR_READ_TIMEOUT_SECONDS: Final = 5.0
"""How long one plain GET among the fixed doors may wait for its next chunk
before it counts as unreachable."""

EVENT_SAMPLE_READ_TIMEOUT_SECONDS: Final = 2.0
"""The read timeout on every chunk of the attention-feed sample; a silent
feed stops the sample rather than the whole reading."""

READER_STOP_GRACE_SECONDS: Final = 5.0
"""How long the reading process may take to end at either step of its
stopping -- to exit after it said it had finished, and to die after it was
told to. Past it the operating system is asked to kill it outright."""

MAXIMUM_RESPONSE_BYTES: Final = 65_536
"""How much of any one fixed door's answer this reading ever buffers."""

EVENT_SAMPLE_MAXIMUM_BYTES: Final = 65_536
EVENT_SAMPLE_MAXIMUM_FRAMES: Final = 20

READER_PROCESS_NAME: Final = "atelier2-watch-reader"

TRANSPORT_LOGGER_NAMES: Final = ("httpx", "httpcore")
"""The libraries the reading process silences in itself: httpx logs every
request's status line -- the far side's own reason phrase included -- at
`INFO`, and httpcore logs a reply's headers at `DEBUG`."""

_SPAWN: Final = get_context("spawn")
"""A fresh interpreter, never a copy of this one: no lock, logger, socket, or
open file of the process that reports is inherited by the process that reads."""

_STANDARD_OUTPUT_DESCRIPTOR: Final = 1
_STANDARD_ERROR_DESCRIPTOR: Final = 2

_SILENT_LEVEL: Final = logging.CRITICAL + 1
"""Above every level `logging` defines, so a logger set to it makes no record
at all."""


@dataclass(frozen=True, slots=True)
class ReadingBudget:
    """The bounds one reading runs under, sent to the process that reads.

    `deadline_seconds` is the whole reading's absolute limit, held by the
    parent process against its own clock, so it is the worst case rather than
    a hope -- what the reading is doing when it falls cannot postpone it. The
    two read timeouts bound one read within it, which is what tells a feed
    that went quiet from one that hangs.
    """

    deadline_seconds: float
    door_read_timeout_seconds: float
    event_sample_read_timeout_seconds: float


WATCH_BUDGET: Final = ReadingBudget(
    deadline_seconds=READING_DEADLINE_SECONDS,
    door_read_timeout_seconds=DOOR_READ_TIMEOUT_SECONDS,
    event_sample_read_timeout_seconds=EVENT_SAMPLE_READ_TIMEOUT_SECONDS,
)


@dataclass(frozen=True, slots=True)
class ReadRefusal:
    """Why one read did not answer, in the client's own fixed words.

    `reason` is a sentence from `atelier_api_client`'s own vocabulary, never a
    library message or a word the far side wrote; `body` is the answer's own
    bytes, kept so a caller can recognize a problem document in them.
    """

    reason: str
    status: int | None = None
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class DoorRead:
    """What one fixed door answered.

    A door the reading never finished sends no record at all -- what the
    deadline interrupted is named by the reading's ending, not by a half
    record.
    """

    endpoint: str
    answer: BoundedRead


@dataclass(frozen=True, slots=True)
class DoorRefused:
    """Why one fixed door's read never reached an answer."""

    endpoint: str
    refusal: ReadRefusal


type DoorAnswer = DoorRead | DoorRefused


@dataclass(frozen=True, slots=True)
class FeedFrame:
    """One decoded `data:` frame of the attention-feed sample, sent the moment
    it was read, with how many bytes the sample had taken by then."""

    data: str
    bytes_read: int


@dataclass(frozen=True, slots=True)
class FeedEnd:
    """How the attention-feed sample ended, and how much it had read."""

    outcome: EventSampleOutcome
    bytes_read: int
    refusal: ReadRefusal | None = None


@dataclass(frozen=True, slots=True)
class ReadingEnd:
    """The reading's last word: every door and the feed were read."""


type ReadingRecord = DoorRead | DoorRefused | FeedFrame | FeedEnd | ReadingEnd


@dataclass(slots=True)
class FeedReading:
    """The attention-feed sample as the reporting side has it so far."""

    frames: list[str] = field(default_factory=list)
    bytes_read: int = 0
    outcome: EventSampleOutcome = EventSampleOutcome.UNREAD
    refusal: ReadRefusal | None = None


@dataclass(slots=True)
class InstanceReading:
    """Every record one reading delivered, and how that reading ended.

    `reader_ended` is the reading's own last word, `deadline_passed` the
    parent's clock, and `reader_exit_code` what the operating system says the
    reading process came back with -- together they are the difference between
    a reading that finished, one that was cut off, and one that died.
    """

    doors: list[DoorAnswer] = field(default_factory=list)
    feed: FeedReading = field(default_factory=FeedReading)
    reader_ended: bool = False
    deadline_passed: bool = False
    reader_exit_code: int | None = None


type ReaderEntry = Callable[[Connection, str, ReadingBudget], None]
"""What a reading process runs: it may send records and it may die, and
either way its whole life is those three arguments."""


def read_instance(service_url: str, budget: ReadingBudget) -> InstanceReading:
    """Read one served instance in a child process, under one deadline.

    An address nothing could ever reach is refused here rather than in the
    child, in the process that can still tell its caller so.
    """

    api_base_url(service_url)
    return supervised_reading(read_into, service_url, budget)


def supervised_reading(
    entry: ReaderEntry, service_url: str, budget: ReadingBudget
) -> InstanceReading:
    """Run `entry` in a spawned child, gather what it sends until the deadline,
    and end it whatever state it is in.

    The deadline is taken before the child is started, so its own startup is
    part of the reading rather than free time added to it. Whatever ends the
    gathering -- the reading's last word, a dead child, the deadline -- the
    child is stopped and reaped before this returns, and the connection is
    closed with it.
    """

    deadline = time.monotonic() + budget.deadline_seconds
    reading = InstanceReading()
    receiver, sender = _SPAWN.Pipe(duplex=False)
    reader = _SPAWN.Process(
        target=entry, args=(sender, service_url, budget), name=READER_PROCESS_NAME
    )
    reader.start()
    # Only the child writes. A copy of the sending end left open here would
    # keep the pipe from ever reaching its end of file, so a child that died
    # without a word would look like one that is still thinking.
    sender.close()
    try:
        _gather(receiver, reading, deadline)
    finally:
        receiver.close()
        _stop(reader, ended=reading.reader_ended)
        reading.reader_exit_code = reader.exitcode
    return reading


def read_into(connection: Connection, service_url: str, budget: ReadingBudget) -> None:
    """The reading process's whole life: go quiet, read, say it is finished.

    Everything this process learns leaves through `connection` and nothing
    else, so its own standard streams are pointed at the null device before
    the first request: a watch's stdout carries one JSON report and nothing
    besides, and httpx would otherwise write the far side's own reason phrase
    into any handler this process happens to have.
    """

    _go_quiet()
    with AtelierApi(service_url) as api:
        send_reading(api, budget, connection.send)
    connection.close()


def send_reading(
    api: AtelierApi, budget: ReadingBudget, send: Callable[[ReadingRecord], None]
) -> None:
    """Read every fixed door and a bounded sample of the feed, sending each
    result the moment it is known.

    Nothing is held back for the end. A reading killed in the middle has
    already delivered every door it finished and every frame it read, which is
    what lets a report be built from a reading that never got to finish.
    """

    for endpoint in DOOR_ENDPOINTS:
        send(_door_answer(api, endpoint, budget))
    send(_sampled_feed(api, budget, send))
    send(ReadingEnd())


def absorb_record(record: ReadingRecord, reading: InstanceReading) -> None:
    """Put one record where the reporting side reads it from."""

    match record:
        case DoorRead() | DoorRefused():
            reading.doors.append(record)
        case FeedFrame():
            reading.feed.frames.append(record.data)
            reading.feed.bytes_read = record.bytes_read
        case FeedEnd():
            reading.feed.outcome = record.outcome
            reading.feed.bytes_read = record.bytes_read
            reading.feed.refusal = record.refusal
        case ReadingEnd():
            reading.reader_ended = True
        case _:
            assert_never(record)


def _gather(receiver: Connection, reading: InstanceReading, deadline: float) -> None:
    """Everything the reading sends until it says it is finished, the pipe
    ends, or the deadline falls."""

    while not reading.reader_ended:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not receiver.poll(remaining):
            reading.deadline_passed = True
            return
        try:
            record: ReadingRecord = receiver.recv()
        except EOFError:
            # The reading process ended without its last word; its exit code
            # is what says so, and the report names it.
            return
        absorb_record(record, reading)


def _stop(reader: SpawnProcess, *, ended: bool) -> None:
    """End the reading process, whatever it is doing.

    A reading that already said it was finished is given its grace to exit on
    its own, so an ordinary ending is never mistaken for a killed one. One
    that has not is past its deadline, and waiting on it would spend the very
    time the deadline was there to bound: it is told to stop at once, and
    killed if it does not. The final join is what reaps it -- no reading
    leaves a process behind.
    """

    if ended:
        reader.join(READER_STOP_GRACE_SECONDS)
    if reader.is_alive():
        reader.terminate()
        reader.join(READER_STOP_GRACE_SECONDS)
    if reader.is_alive():
        reader.kill()
    reader.join()


def _door_answer(api: AtelierApi, endpoint: str, budget: ReadingBudget) -> DoorAnswer:
    try:
        answer = api.bounded_get(
            endpoint,
            read_timeout_seconds=budget.door_read_timeout_seconds,
            maximum_bytes=MAXIMUM_RESPONSE_BYTES,
        )
    except AtelierApiTransportFailure as failure:
        return DoorRefused(endpoint, _refusal(failure))
    return DoorRead(endpoint, answer)


def _sampled_feed(
    api: AtelierApi, budget: ReadingBudget, send: Callable[[ReadingRecord], None]
) -> FeedEnd:
    tally = EventSampleTally()
    try:
        api.sampled_event_frames(
            EVENTS_PATH,
            accept=EVENT_STREAM_MEDIA_TYPE,
            read_timeout_seconds=budget.event_sample_read_timeout_seconds,
            maximum_bytes=EVENT_SAMPLE_MAXIMUM_BYTES,
            maximum_frames=EVENT_SAMPLE_MAXIMUM_FRAMES,
            on_frame=lambda data: send(FeedFrame(data, tally.bytes_read)),
            tally=tally,
        )
    except AtelierApiTransportFailure as failure:
        return FeedEnd(
            _refused_feed_outcome(failure, tally),
            tally.bytes_read,
            refusal=_refusal(failure),
        )
    return FeedEnd(tally.outcome, tally.bytes_read)


def _refused_feed_outcome(
    failure: AtelierApiTransportFailure, tally: EventSampleTally
) -> EventSampleOutcome:
    """A service that answered nothing at all is unreachable; one that sent a
    status or bytes and was then refused by this client is not."""

    if failure.status is not None or tally.bytes_read > 0:
        return EventSampleOutcome.REFUSED
    return EventSampleOutcome.UNREACHABLE


def _refusal(failure: AtelierApiTransportFailure) -> ReadRefusal:
    return ReadRefusal(failure.reason, status=failure.status, body=failure.body)


def _go_quiet() -> None:
    """Leave this process no channel but its pipe.

    The two libraries are silenced at their own loggers rather than at a
    handler: a level set here is the effective level of every logger beneath
    it -- `httpcore.http11` included -- so no record of either family is ever
    made, and a handler one of them installed on itself would have nothing to
    carry. The root keeps a handler that drops whatever else this process
    might say, and the standard descriptors are pointed at the null device
    rather than only `sys.stdout`, so a write from below Python goes nowhere
    either.
    """

    logging.getLogger().handlers = [logging.NullHandler()]
    for name in TRANSPORT_LOGGER_NAMES:
        logging.getLogger(name).setLevel(_SILENT_LEVEL)
    null_device = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null_device, _STANDARD_OUTPUT_DESCRIPTOR)
    os.dup2(null_device, _STANDARD_ERROR_DESCRIPTOR)
    os.close(null_device)
