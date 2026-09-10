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
diagnosis (`AtelierApiTransportFailure.reason`) before it is ever sent, and an
unexpected failure becomes a phase and a category. Each record leaves the
moment it is known, so a reading the deadline ends has already delivered every
door and every frame it read.

Both ends frame the pipe themselves -- a length and a payload -- rather than
using the connection's own `send`/`recv`. A reading readable at this end is
not a whole record: `recv` would block past the deadline waiting for the rest,
and a reader dying mid-payload would raise instead of reporting. The parent
reads without blocking against the time it has left, keeps what it has, and
decodes only complete frames; a half frame is simply never a record.

Because the child is a spawned interpreter, a library caller of
`read_instance` needs an importable main module (`atelier2/__main__.py` guards
its own entry for exactly this reason).
"""

from __future__ import annotations

import ctypes
import logging
import os
import pickle
import select
import signal
import struct
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
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
    TransportFailureCategory,
    api_base_url,
    failure_category,
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

READER_STOP_GRACE_SECONDS: Final = 0.5
"""How long the reading process is waited for at each step of its stopping --
to exit after it said it had finished, to die after it was told to, and to be
reaped after it was killed. Short, and never open-ended: a stop that waited on
a process that will not go would spend the very time the deadline bounds."""

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

_PARENT_DEATH_SIGNAL_OPTION: Final = 1
"""`PR_SET_PDEATHSIG` (`linux/prctl.h`), the same arming
`adapters/agent_process_exec_guard.py` gives an agent's own child."""

_FRAME_HEADER: Final = struct.Struct("!I")
"""How long the payload after it is: the whole framing, on both ends."""

_READ_CHUNK_BYTES: Final = 65_536
"""How much of the pipe one read takes at a time; a frame is reassembled from
however many of these it spans."""


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


class ReaderPhase(StrEnum):
    """Where in its own life the reading process ran into trouble.

    The distinction the report needs: trouble before or during the reading
    means doors went unread, while trouble putting the client away happened
    after every door was already reported.
    """

    SETUP = "setup"
    READING = "reading"
    CLEANUP = "cleanup"


@dataclass(frozen=True, slots=True)
class ReaderFailure:
    """The reading process ran into something it did not expect.

    Two fixed words, never a message: an unexpected exception's text is the
    one place a library or the far side could still write into this report,
    and its class alone is what `failure_category` turns into a word this
    repository owns.
    """

    phase: ReaderPhase
    category: TransportFailureCategory


type ReadingRecord = (
    DoorRead | DoorRefused | FeedFrame | FeedEnd | ReaderFailure | ReadingEnd
)


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

    `reader_ended` is the reading's own last word, `failure` what it said went
    wrong before it stopped saying anything, `deadline_passed` the parent's
    clock, `reader_exit_code` what the operating system says the process came
    back with, and `reader_reaped` whether it went at all -- together they are
    the difference between a reading that finished, one that was cut off, one
    that broke, and one that would not die.
    """

    doors: list[DoorAnswer] = field(default_factory=list)
    feed: FeedReading = field(default_factory=FeedReading)
    reader_ended: bool = False
    failure: ReaderFailure | None = None
    deadline_passed: bool = False
    reader_exit_code: int | None = None
    reader_reaped: bool = True


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
    part of the reading rather than free time added to it -- and a deadline
    already spent starts no process at all. Whatever ends the gathering, the
    child is stopped, reaped, and reported on before this returns, and the
    pipe is closed only once no process could still be writing into it.
    """

    deadline = time.monotonic() + budget.deadline_seconds
    reading = InstanceReading()
    if _left_of(deadline) <= 0:
        reading.deadline_passed = True
        return reading
    receiver, sender = _SPAWN.Pipe(duplex=False)
    reader = _SPAWN.Process(
        target=entry,
        args=(sender, service_url, budget),
        name=READER_PROCESS_NAME,
        # A reading outlives nothing: a parent that exits takes its daemonic
        # children with it, and `_die_with_the_parent` covers the parent that
        # does not get to exit at all.
        daemon=True,
    )
    try:
        reader.start()
        # Only the child writes. A copy of the sending end left open here
        # would keep the pipe from ever reaching its end of file, so a child
        # that died without a word would look like one still thinking.
        sender.close()
        try:
            _gather(receiver.fileno(), reading, deadline)
        finally:
            reading.reader_reaped = _stopped(
                reader, deadline, finished=not reading.deadline_passed
            )
            reading.reader_exit_code = reader.exitcode
    finally:
        sender.close()
        receiver.close()
    return reading


def read_into(connection: Connection, service_url: str, budget: ReadingBudget) -> None:
    """The reading process's whole life: go quiet, read, say what happened.

    Everything this process learns leaves through `connection` and nothing
    else, so its own standard streams are pointed at the null device before
    the first request: a watch's stdout carries one JSON report and nothing
    besides, and httpx would otherwise write the far side's own reason phrase
    into any handler this process happens to have.

    Nothing here is allowed to end in a traceback nobody reads. Trouble the
    reading did not expect becomes a record naming the phase it happened in,
    and this process then exits cleanly, because a reader that died and a
    reader that broke are two different findings.
    """

    _go_quiet()
    _die_with_the_parent()
    send = _record_sink(connection)
    api = _opened(service_url, send)
    if api is not None:
        try:
            send_reading(api, budget, send)
        except Exception as trouble:
            send(_reader_failure(ReaderPhase.READING, trouble))
        finally:
            _closed(api, send)
    connection.close()


def _opened(
    service_url: str, send: Callable[[ReadingRecord], None]
) -> AtelierApi | None:
    try:
        return AtelierApi(service_url)
    except Exception as trouble:
        send(_reader_failure(ReaderPhase.SETUP, trouble))
        return None


def _closed(api: AtelierApi, send: Callable[[ReadingRecord], None]) -> None:
    """Putting the client away is its own phase: a failure here happened after
    every door was already reported, and saying so is what keeps it from
    reading as a reading that never finished."""

    try:
        api.close()
    except Exception as trouble:
        send(_reader_failure(ReaderPhase.CLEANUP, trouble))


def _reader_failure(phase: ReaderPhase, trouble: BaseException) -> ReaderFailure:
    return ReaderFailure(phase, failure_category(trouble))


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
            # A frame is proof the sample ran; how it ended is only known when
            # its end arrives, so until then the feed stands as interrupted
            # rather than as never read.
            reading.feed.outcome = EventSampleOutcome.INTERRUPTED
        case FeedEnd():
            reading.feed.outcome = record.outcome
            reading.feed.bytes_read = record.bytes_read
            reading.feed.refusal = record.refusal
        case ReaderFailure():
            # The first one is the one that explains the rest.
            reading.failure = reading.failure or record
        case ReadingEnd():
            reading.reader_ended = True
        case _:
            assert_never(record)


def _record_sink(connection: Connection) -> Callable[[ReadingRecord], None]:
    """Where the reading process puts a record: one framed payload on the
    pipe, written whole before the next one starts."""

    descriptor = connection.fileno()

    def send(record: ReadingRecord) -> None:
        payload = pickle.dumps(record, protocol=pickle.HIGHEST_PROTOCOL)
        _written(descriptor, _FRAME_HEADER.pack(len(payload)) + payload)

    return send


def _written(descriptor: int, frame: bytes) -> None:
    written = 0
    while written < len(frame):
        written += os.write(descriptor, frame[written:])


def _gather(descriptor: int, reading: InstanceReading, deadline: float) -> None:
    """Everything the reading sends until it says it is finished, the pipe
    ends, or the deadline falls.

    Read without blocking, against the time the reading has left: what is
    readable is whatever the child has written so far, not necessarily a whole
    record, and waiting for the rest of a half-written one is exactly what a
    deadline must never do. A pipe that ends -- because the reader is gone --
    ends the gathering; it is never an error here, because the process's own
    exit is what says what happened to it.
    """

    os.set_blocking(descriptor, False)
    buffer = bytearray()
    while not reading.reader_ended:
        left = _left_of(deadline)
        if left <= 0 or not select.select([descriptor], [], [], left)[0]:
            reading.deadline_passed = True
            return
        try:
            arrived = os.read(descriptor, _READ_CHUNK_BYTES)
        except BlockingIOError:
            continue
        except OSError:
            return
        if not arrived:
            return
        buffer.extend(arrived)
        for record in _complete_records(buffer):
            absorb_record(record, reading)


def _complete_records(buffer: bytearray) -> Iterator[ReadingRecord]:
    """Every whole frame in `buffer`, taken out of it as it is decoded; a
    partial one stays for the bytes that would complete it."""

    while len(buffer) >= _FRAME_HEADER.size:
        (length,) = _FRAME_HEADER.unpack_from(buffer)
        end = _FRAME_HEADER.size + length
        if len(buffer) < end:
            return
        payload = bytes(buffer[_FRAME_HEADER.size : end])
        del buffer[:end]
        yield pickle.loads(payload)


def _stopped(reader: SpawnProcess, deadline: float, *, finished: bool) -> bool:
    """End the reading process, whatever it is doing, and say whether it went.

    `finished` is what the pipe said: a last word, or its end. Such a reading
    is already on its way out and gets what is left of the deadline, capped at
    one grace, to get there on its own -- so an ordinary ending is never
    mistaken for a killed one. One the deadline interrupted gets none of that:
    it is told to stop, then killed, each with one short bounded wait, because
    waiting here would spend the very time the deadline bounds. A process that
    survives even a kill is not waited for; it is reported.
    """

    if finished:
        reader.join(min(READER_STOP_GRACE_SECONDS, _left_of(deadline)))
    if reader.is_alive():
        reader.terminate()
        reader.join(READER_STOP_GRACE_SECONDS)
    if reader.is_alive():
        reader.kill()
        reader.join(READER_STOP_GRACE_SECONDS)
    return not reader.is_alive()


def _left_of(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


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

    This process is private: it reports through its pipe and has no reader for
    anything else, so nothing in it may log at all. `logging.disable` is what
    makes that true whoever asks -- a level on the two library loggers is
    walked past by a child logger that sets its own, and a handler hung
    directly on `httpcore.http11` would then carry the far side's headers to
    wherever it points. The root keeps a handler that drops what is left, and
    the standard descriptors are pointed at the null device rather than only
    `sys.stdout`, so a write from below Python goes nowhere either.
    """

    logging.disable(logging.CRITICAL)
    logging.getLogger().handlers = [logging.NullHandler()]
    for name in TRANSPORT_LOGGER_NAMES:
        logging.getLogger(name).setLevel(_SILENT_LEVEL)
    null_device = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null_device, _STANDARD_OUTPUT_DESCRIPTOR)
    os.dup2(null_device, _STANDARD_ERROR_DESCRIPTOR)
    os.close(null_device)


def _die_with_the_parent() -> None:
    """Ask the kernel to kill this process when the process that reads its
    report is gone.

    `daemon=True` covers a parent that exits; this covers one that is killed
    and never gets to. The same arming an agent's own child gets before it
    execs (`adapters/agent_process_exec_guard.py`), which cannot be called
    here: that function never returns, and wants a cgroup and a watchdog this
    reading has neither of.
    """

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PARENT_DEATH_SIGNAL_OPTION, signal.SIGKILL) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
