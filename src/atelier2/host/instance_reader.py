"""The reading half of `atelier2 watch`: a child process reads a served
instance and streams what it saw, record by record.

Why a process rather than a timer. Every call this reading makes goes through
httpx, and a deadline that has to interrupt a blocking read from inside the
process can only do it by throwing into somebody else's code: an exception
landing in a connection pool's own critical section leaves that lock held, and
the observer deadlocks before it can report. Here the parent holds one end of
a pipe and a clock, the child holds the client and every lock it needs, and a
child still reading when the deadline falls is terminated, killed if it must
be, and reclaimed by the operating system with everything it held.

The child is an ordinary interpreter this module starts and owns
(`subprocess.Popen` on `instance_reader_main`), never a
`multiprocessing.Process`: that one registers every child it starts and joins
the survivors without a timeout as the process exits, so a reader that
outlived its kill would hang the very command that had already reported it.
Here nothing is registered, every wait carries a timeout, and a survivor is
reported and left.

Nothing but records crosses the pipe: typed, picklable, and carrying no
exception object, because a library's exception carries its own message and
the far side's text with it. Each leaves the moment it is known, so a reading
the deadline ends has already delivered every door and frame it read.

Both ends frame the pipe themselves -- a length and a payload -- because what
is readable at the reporting end is not necessarily a whole record, and
waiting for the rest of one is what a deadline must never do. The parent reads
without blocking against the time it has left and decodes only complete
frames; a half frame is never a record, and a length no record of this reading
could carry ends the gathering with a named failure.
"""

from __future__ import annotations

import ctypes
import logging
import os
import pickle
import select
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
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
started: wide enough that each door's own read timeout normally ends the
reading, and narrower than an operator's patience with a trickling instance."""

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

READER_MODULE: Final = "atelier2.host.instance_reader_main"
"""What a reading process runs: the entry that reads this reading's arguments
and hands them here. A caller that runs another module in its place gets the
same protocol on the same pipe."""

_INTERRUPT_SIGNALS: Final = frozenset({signal.SIGINT})
"""What is held off while a reading process is started: an interrupt delivered
between the process existing and this one holding its handle would leave a
reading nobody could stop."""

TRANSPORT_LOGGER_NAMES: Final = ("httpx", "httpcore")
"""The libraries the reading process silences in itself: httpx logs every
request's status line -- the far side's own reason phrase included -- at
`INFO`, and httpcore logs a reply's headers at `DEBUG`."""

FRAME_HEADER: Final = struct.Struct("!I")
"""How long the payload after it is: the whole framing, on both ends."""

FRAME_LIMIT_BYTES: Final = 4 * MAXIMUM_RESPONSE_BYTES
"""The largest payload any record of this reading can have -- a door's whole
answer with room for what a record carries around it. A length beyond it, or
of nothing at all, is not this reading's writing."""

_NO_DESCRIPTOR: Final = -1
"""What a descriptor this process has already handed over reads as."""

_NO_PIPE_EXIT_CODE: Final = 2
"""How a reading process ends when it has no pipe to report through: there is
nothing it could say and nobody who would hear it."""

_ORPHANED_EXIT_CODE: Final = 0
"""How a reading process ends when the process that would read its report is
already gone: quietly, because nothing it did would be looked at."""

_SILENT_LEVEL: Final = logging.CRITICAL + 1
"""Above every level `logging` defines, so a logger set to it makes no record
at all."""

_PARENT_DEATH_SIGNAL_OPTION: Final = 1
"""`PR_SET_PDEATHSIG` (`linux/prctl.h`), the same arming
`adapters/agent_process_exec_guard.py` gives an agent's own child."""

_READ_CHUNK_BYTES: Final = 65_536
"""How much of the pipe one read takes; a frame spans however many it needs."""


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

    The reading's own last word, what it said went wrong, the parent's clock,
    what the operating system says the process came back with, and whether it
    went at all: together the difference between a reading that finished, one
    that was cut off, one that broke, and one that would not die.
    """

    doors: list[DoorAnswer] = field(default_factory=list)
    feed: FeedReading = field(default_factory=FeedReading)
    reader_ended: bool = False
    failure: ReaderFailure | None = None
    deadline_passed: bool = False
    reader_exit_code: int | None = None
    reader_reaped: bool = True


@dataclass(frozen=True, slots=True)
class ReaderInvocation:
    """What one reading process is told when it starts: where to read, what to
    write its records to, and whose child it is."""

    service_url: str
    budget: ReadingBudget
    descriptor: int
    parent_process_id: int


def read_instance(service_url: str, budget: ReadingBudget) -> InstanceReading:
    """Read one served instance in a child process, under one deadline.

    An address nothing could ever reach is refused here rather than in the
    child, in the process that can still tell its caller so.
    """

    api_base_url(service_url)
    return supervised_reading(READER_MODULE, service_url, budget)


def supervised_reading(
    reader_module: str, service_url: str, budget: ReadingBudget
) -> InstanceReading:
    """Run `reader_module` as a reading process, gather what it sends until the
    deadline, and end it whatever state it is in.

    The deadline is taken before the process is started, so its own startup is
    part of the reading rather than free time added to it -- and a deadline
    already spent starts nothing at all. The `finally` covers everything from
    before the start onwards, so an interruption anywhere in between still
    ends the reading, and the pipe closes whatever that stopping does. The
    writing end is closed here as soon as the child has it, because a copy
    left open would keep the pipe from ever reaching its end and make a dead
    reader look like a thinking one.

    That process has one channel and it is that pipe: its standard streams go
    to the null device, so a `watch` prints one JSON report and nothing
    besides, whatever the reading or a library under it would otherwise write.
    """

    deadline = time.monotonic() + budget.deadline_seconds
    reading = InstanceReading()
    if _left_of(deadline) <= 0:
        reading.deadline_passed = True
        return reading
    reading_end, sending_end = os.pipe()
    reader: subprocess.Popen[bytes] | None = None
    try:
        # An interrupt landing between the process existing and this name
        # holding it would leave a reading nobody could stop, so it is held
        # off until the handle is bound and delivered the moment it is.
        held = signal.pthread_sigmask(signal.SIG_BLOCK, _INTERRUPT_SIGNALS)
        try:
            reader = subprocess.Popen(
                _reader_command(reader_module, service_url, budget, sending_end),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(sending_end,),
            )
            os.close(sending_end)
            sending_end = _NO_DESCRIPTOR
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)
        _gather(reading_end, reading, deadline)
    finally:
        try:
            if reader is not None:
                reading.reader_reaped = _stopped(
                    reader, deadline, finished=not reading.deadline_passed
                )
                reading.reader_exit_code = reader.returncode
        finally:
            for descriptor in (sending_end, reading_end):
                _given_back(descriptor)
    return reading


def _given_back(descriptor: int) -> None:
    """Close a descriptor this reading still holds, and count one that is
    already gone as closed: the closing runs to its end, so trouble over the
    first descriptor cannot leave the second one behind."""

    if descriptor == _NO_DESCRIPTOR:
        return
    try:
        os.close(descriptor)
    except OSError:
        return


def _reader_command(
    reader_module: str, service_url: str, budget: ReadingBudget, descriptor: int
) -> list[str]:
    """How a reading process is asked for: this interpreter, that module, and
    everything it needs as arguments -- nothing inherited, nothing implied.

    The other half of this protocol is `instance_reader_main`, which reads
    these arguments back in the process that was started with them.
    """

    return [
        sys.executable,
        "-m",
        reader_module,
        "--service",
        service_url,
        "--descriptor",
        str(descriptor),
        "--parent",
        str(os.getpid()),
        "--deadline-seconds",
        str(budget.deadline_seconds),
        "--door-read-timeout-seconds",
        str(budget.door_read_timeout_seconds),
        "--event-sample-read-timeout-seconds",
        str(budget.event_sample_read_timeout_seconds),
    ]


def read_as_a_child(invocation: ReaderInvocation) -> int:
    """The reading process's whole life: go quiet, read, say what happened.

    Nothing here is allowed to end in a traceback nobody reads. Trouble it did
    not expect becomes a record naming the phase it happened in, and this
    process then exits cleanly, because a reader that died and a reader that
    broke are two different findings. Only the pipe itself comes before that:
    with no way to say anything, there is nothing to say and nobody to hear
    it.
    """

    try:
        send = record_sink(invocation.descriptor)
    except OSError:
        os._exit(_NO_PIPE_EXIT_CODE)
    api = _prepared(invocation, send)
    if api is None:
        return 0
    everything_read = True
    try:
        send_reading(api, invocation.budget, send)
    except Exception as trouble:
        send(_reader_failure(ReaderPhase.READING, trouble))
        everything_read = False
    # The last word comes after the client is away, so a cleanup that failed
    # can never leave a report that looks complete.
    if _closed(api, send) and everything_read:
        send(ReadingEnd())
    return 0


def _prepared(
    invocation: ReaderInvocation, send: Callable[[ReadingRecord], None]
) -> AtelierApi | None:
    """Everything this process does to itself before it reads anything, inside
    the same translation as the reading: going quiet and arming its own death
    can fail too, and a failure there is a record like any other."""

    try:
        _go_quiet()
        _die_with_the_parent(invocation.parent_process_id)
        return AtelierApi(invocation.service_url)
    except Exception as trouble:
        send(_reader_failure(ReaderPhase.SETUP, trouble))
        return None


def _closed(api: AtelierApi, send: Callable[[ReadingRecord], None]) -> bool:
    """Whether the client went away cleanly.

    Putting it away is its own phase, and it happens before the reading's last
    word, so a cleanup that failed cannot leave a report that reads as
    complete.
    """

    try:
        api.close()
    except Exception as trouble:
        send(_reader_failure(ReaderPhase.CLEANUP, trouble))
        return False
    return True


def _reader_failure(phase: ReaderPhase, trouble: BaseException) -> ReaderFailure:
    return ReaderFailure(phase, failure_category(trouble))


def send_reading(
    api: AtelierApi, budget: ReadingBudget, send: Callable[[ReadingRecord], None]
) -> None:
    """Read every fixed door and a bounded sample of the feed, sending each
    result the moment it is known.

    Nothing is held back for the end -- a reading killed in the middle has
    already delivered every door it finished and every frame it read -- and
    the last word is not sent here: it belongs after the client is away.
    """

    for endpoint in DOOR_ENDPOINTS:
        send(_door_answer(api, endpoint, budget))
    send(_sampled_feed(api, budget, send))


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


def framed(record: ReadingRecord) -> bytes:
    """One record as it travels: how long its payload is, then its payload."""

    payload = pickle.dumps(record, protocol=pickle.HIGHEST_PROTOCOL)
    return FRAME_HEADER.pack(len(payload)) + payload


def record_sink(descriptor: int) -> Callable[[ReadingRecord], None]:
    """Where the reading process puts a record: one framed payload on the
    pipe, written whole before the next one starts.

    The descriptor is asked about before anything is written to it, so a
    reading with no way to report finds that out while it still could have
    done something else about it.
    """

    os.fstat(descriptor)

    def send(record: ReadingRecord) -> None:
        _written(descriptor, framed(record))

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
    deadline must never do. A descriptor that is no longer one ends the
    gathering where it stands, here as everywhere else: losing the pipe is an
    end to what can still arrive, never a crash of the process that reports.
    """

    try:
        os.set_blocking(descriptor, False)
    except OSError:
        return
    buffer = bytearray()
    while not reading.reader_ended:
        arrived = _arrived(descriptor, _left_of(deadline), reading)
        if arrived is None:
            return
        if not arrived:
            continue
        buffer.extend(arrived)
        try:
            for record in _complete_records(buffer):
                absorb_record(record, reading)
        except _FramingCorrupt:
            absorb_record(_IPC_CORRUPT_FAILURE, reading)
            return


def _arrived(descriptor: int, left: float, reading: InstanceReading) -> bytes | None:
    """Whatever the reading has written and this end has not taken yet.

    `None` means the gathering is over: the deadline fell, the pipe reached
    its end because the reader is gone, or the descriptor itself is no longer
    one -- none of which is an error here, because the reading process's own
    exit is what says what happened to it. Empty bytes mean nothing was ready
    after all and the next look is due.
    """

    if left <= 0:
        reading.deadline_passed = True
        return None
    try:
        if not select.select([descriptor], [], [], left)[0]:
            reading.deadline_passed = True
            return None
        return os.read(descriptor, _READ_CHUNK_BYTES) or None
    except BlockingIOError:
        return b""
    except OSError:
        return None


class _FramingCorrupt(Exception):
    """The pipe carried something no record of this reading could have
    written; never raised past this module."""


_IPC_CORRUPT_FAILURE: Final = ReaderFailure(
    ReaderPhase.READING, TransportFailureCategory.IPC_CORRUPT
)
"""What the reporting side records when the framing itself stops making
sense: the reading is over, and it is not a clean one."""


def _complete_records(buffer: bytearray) -> Iterator[ReadingRecord]:
    """Every whole frame in `buffer`, taken out of it as it is decoded; a
    partial one stays for the bytes that would complete it.

    A length of nothing, a length beyond anything this reading writes, or a
    payload that does not decode is not a record this end waits for: it is a
    pipe that has stopped making sense, and the gathering ends on it.
    """

    while len(buffer) >= FRAME_HEADER.size:
        (length,) = FRAME_HEADER.unpack_from(buffer)
        if not 0 < length <= FRAME_LIMIT_BYTES:
            raise _FramingCorrupt
        end = FRAME_HEADER.size + length
        if len(buffer) < end:
            return
        payload = bytes(buffer[FRAME_HEADER.size : end])
        del buffer[:end]
        yield _decoded(payload)


def _decoded(payload: bytes) -> ReadingRecord:
    try:
        return pickle.loads(payload)
    except Exception:
        raise _FramingCorrupt from None


def _stopped(
    reader: subprocess.Popen[bytes], deadline: float, *, finished: bool
) -> bool:
    """End the reading process, whatever it is doing, and say whether it went.

    `finished` is what the pipe said: a last word, or its end. Such a reading
    is already on its way out and gets what is left of the deadline, capped at
    one grace, to get there on its own -- so an ordinary ending is never
    mistaken for a killed one. One the deadline interrupted gets none of that:
    it is told to stop, then killed, each with one short bounded wait, because
    waiting here would spend the very time the deadline bounds. Every step
    stands in the `finally` of the one before it, so an interruption while
    waiting cannot skip the harder step behind it, and a process that survives
    even a kill is reported rather than waited for.
    """

    try:
        if finished:
            _waited(reader, min(READER_STOP_GRACE_SECONDS, _left_of(deadline)))
    finally:
        try:
            if reader.poll() is None:
                reader.terminate()
                _waited(reader, READER_STOP_GRACE_SECONDS)
        finally:
            if reader.poll() is None:
                reader.kill()
                _waited(reader, READER_STOP_GRACE_SECONDS)
    return reader.poll() is not None


def _waited(reader: subprocess.Popen[bytes], seconds: float) -> None:
    """Give the reading process `seconds` to be gone, and no more."""

    try:
        reader.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return


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
    """Leave this process nothing to say anywhere but its pipe.

    This process is private: it reports through its pipe and has no reader for
    anything else, so nothing in it may log at all. `logging.disable` is what
    makes that true whoever asks -- a level on the two library loggers is
    walked past by a child logger that sets its own, and a handler hung
    directly on `httpcore.http11` would then carry the far side's headers to
    wherever it points -- and the root keeps a handler that drops what is
    left. Where a write below Python would land is not decided here: the
    standard streams of a reading process belong to whoever starts it, and
    `supervised_reading` gives them the null device.
    """

    logging.disable(logging.CRITICAL)
    logging.getLogger().handlers = [logging.NullHandler()]
    for name in TRANSPORT_LOGGER_NAMES:
        logging.getLogger(name).setLevel(_SILENT_LEVEL)


def _die_with_the_parent(parent_process_id: int) -> None:
    """Ask the kernel to kill this process when the process that reads its
    report is gone, and leave at once if it already is.

    A parent that died between this process starting and this arming would
    leave the arming pointing at whoever adopted this process instead -- an
    ask that would never come -- so the parent is checked again afterwards,
    and a reading nobody is waiting for ends here without a word.

    The same arming an agent's own child gets before it execs
    (`adapters/agent_process_exec_guard.py`), which cannot be called here:
    that function never returns, and wants a cgroup and a watchdog this
    reading has neither of.
    """

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PARENT_DEATH_SIGNAL_OPTION, signal.SIGKILL) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    if os.getppid() != parent_process_id:
        os._exit(_ORPHANED_EXIT_CODE)
