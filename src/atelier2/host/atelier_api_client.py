"""One `httpx`-backed client every host command speaks the Atelier API through.

`AtelierApi` validates the address a caller named, holds one `httpx.Client`
for the life of one invocation, and turns every transport-level problem --
a non-2xx answer, an unreachable service, or a stream that stops decoding as
text -- into one typed `AtelierApiTransportFailure`. A caller closes the
client when its invocation ends (a context manager, or `close()`), and
translates that one failure into whatever vocabulary its own callers expect.

A caller that does not trust the far side reads through `reading_client`
instead: one client, one deadline for every call it makes together, and no
touch of that client once the deadline has fired.

No text an answer wrote ever reaches a failure raised here: not a reason
phrase, not a served header value, and not a library exception's own message
-- every translation is `from None`, so a traceback cannot carry one out
either, and what it says instead comes from `TransportFailureCategory`.
"""

from __future__ import annotations

import signal
import socket
import ssl
import threading
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from types import FrameType
from typing import Final, Self
from urllib.parse import urlsplit

import httpx

from atelier2.api.openapi import API_PREFIX
from atelier2.host.address import ADDRESSABLE_SCHEMES, DEFAULT_SERVICE_URL

JSON_MEDIA_TYPE = "application/json"

EVENT_STREAM_MEDIA_TYPE: Final = "text/event-stream"

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
"""The one content type other than an event stream whose body a sample still
reads: this API's own problem document, which is how a route caught before it
could start streaming says what went wrong."""

MAXIMUM_FAILURE_BODY_BYTES = 4_096
"""How much of a non-2xx or malformed answer a caller reads before giving up
on classifying it further; past this, further bytes buy nothing but memory."""

_IDENTITY_ENCODING_HEADERS: Final = {"accept-encoding": "identity"}
"""A bounded read asks for the wire bytes as they are: a compressed reply
could expand past this call's byte cap between the wire and the buffer,
after the cap already thought it was safe."""

_UNEXPECTED_ENCODING_REASON: Final = (
    "answered with a content-encoding other than identity, refused before "
    "reading its body"
)

_NOT_EVENT_STREAM_REASON: Final = (
    "answered a content type that is not an event stream, refused before "
    "reading its body"
)

_BYTE_LIMIT_REASON: Final = "a bounded read gave up: byte limit"


class AtelierApiAddressUnusable(ValueError):
    """`service_url` names nothing this client could ever reach."""


class WallClockDeadlineExceeded(Exception):
    """One reading phase's absolute deadline ran out while it was still reading.

    Raised on the calling thread by the deadline's own alarm, so it interrupts
    whichever blocking read that thread sits in -- headers that never
    complete, a socket that never speaks again. It ends the whole phase: see
    `reading_client` for why the client it interrupted is then never touched
    again.
    """


class WallClockDeadlineUnavailable(RuntimeError):
    """A reading phase asked for a deadline this thread cannot be given.

    Only the main thread receives the process's alarm, so anywhere else this
    refuses rather than running an unbounded read that merely looks bounded.
    """


class WallClockDeadlineAlreadyArmed(RuntimeError):
    """A deadline was asked for while this process's interval timer runs.

    One process has one `ITIMER_REAL`: a second deadline would replace the
    first one's expiry and hand its own handler back on the way out, leaving
    the block that believed itself bounded running with no alarm at all.
    """


@contextmanager
def wall_clock_deadline(seconds: float) -> Iterator[None]:
    """Stop whatever the calling thread is doing once `seconds` have passed.

    A read timeout bounds one read; this bounds the whole block, including a
    reply whose headers never finish arriving and a read that never returns
    at all. Arming happens inside the block's own `try`, and the timer is
    disarmed before the previous handler goes back -- an alarm already queued
    at that moment is delivered at the next bytecode boundary, which can fall
    inside the disarming itself, so the disarming is repeated rather than
    leaving this module's handler installed for whatever runs next.
    """

    if threading.current_thread() is not threading.main_thread():
        raise WallClockDeadlineUnavailable(
            "a bounded read's deadline is enforced by this process's alarm, "
            "which only the main thread receives"
        )
    already_running, _ = signal.getitimer(signal.ITIMER_REAL)
    if already_running:
        raise WallClockDeadlineAlreadyArmed(
            "this process's interval timer is already running; a second "
            "deadline would silently replace it"
        )
    if seconds <= 0:
        raise WallClockDeadlineExceeded

    def expire(signal_number: int, frame: FrameType | None) -> None:
        del signal_number, frame
        raise WallClockDeadlineExceeded

    previous_handler = signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
        except WallClockDeadlineExceeded:
            # This block's own alarm was already queued when it was disarmed.
            # Python delivers it at the next bytecode boundary, which can fall
            # inside the disarming itself; one alarm can be pending, so one
            # repetition finishes what the throw interrupted.
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)


@contextmanager
def reading_client(
    service_url: str,
    deadline_seconds: float,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[AtelierApi]:
    """One client for one reading phase, under one deadline, and none after it.

    The deadline fires on this thread, inside whichever blocking call httpx
    was in, so the client is left at an unknown point: possibly between
    marking a response closed and releasing its socket, possibly holding a
    connection pool's lock. Nothing touches it again -- `close()` included,
    which could block on that very lock -- and the phase ends with whatever
    its caller had already collected. The client is created here and nowhere
    else so that this is structural rather than remembered: no reference to a
    client a deadline abandoned outlives this block. Any other exception
    leaving the block skips the close for the same reason it ends the one-shot
    command that opened it.
    """

    api = AtelierApi(service_url, transport=transport)
    with wall_clock_deadline(deadline_seconds):
        yield api
    api.close()


class EventSampleOutcome(StrEnum):
    """How one bounded look at an event stream ended.

    The read writes `INTERRUPTED` before its first byte and replaces it on the
    way out with whichever of the next four stopped it, so a read a deadline
    ended never claims an ending it did not reach. `REFUSED` and
    `UNREACHABLE` are written by the caller that catches this client's typed
    failure, and `UNREAD` means the sample never ran at all.
    """

    UNREAD = "unread"
    INTERRUPTED = "interrupted"
    SILENT = "silent"
    FRAME_LIMIT = "frame-limit"
    BYTE_LIMIT = "byte-limit"
    CLOSED_EARLY = "closed-early"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"


@dataclass(slots=True)
class EventFrameCollection:
    """Every frame one sampled event stream decoded, and how the sample ended.

    Mutable, and the caller keeps its own reference: the sample writes each
    frame and each byte count as it reads them, so whatever stops the read --
    its own caps, a typed failure, or the reading phase's deadline firing
    inside it -- leaves the caller holding everything already seen.
    """

    frames: list[str] = field(default_factory=list)
    bytes_read: int = 0
    outcome: EventSampleOutcome = EventSampleOutcome.UNREAD


@dataclass(slots=True)
class BoundedRead:
    """One bounded GET's answer, as far as the read got.

    Mutable for the same reason `EventFrameCollection` is: `status` lands as
    soon as the headers do and `body` grows chunk by chunk, so a read the
    deadline ended still says what it had. A non-2xx status is not raised
    here -- a caller reading an instance it does not yet trust classifies the
    body itself, whichever status carried it.
    """

    status: int | None = None
    body: bytearray = field(default_factory=bytearray)


class _ByteCapExceeded(Exception):
    """Raised inside a bounded read's own chunk generator once the next chunk
    would push it past its cap; never escapes this module."""


class AtelierApiTransportFailure(Exception):
    """One HTTP call to the Atelier API did not answer as this client asked.

    `status` is `None` for a connection or timeout failure that reached no
    response at all, or for a stream this client could not read as text --
    `reason` then names the complaint. Otherwise `status` is the non-2xx
    status the service did answer with, `reason` its bare HTTP reason
    phrase, and `body` its response bytes, bounded to
    `MAXIMUM_FAILURE_BODY_BYTES`.
    """

    def __init__(
        self, url: str, reason: str, *, status: int | None = None, body: bytes = b""
    ) -> None:
        super().__init__(reason)
        self.url = url
        self.reason = reason
        self.status = status
        self.body = body


def api_base_url(service_url: str) -> str:
    """Where this client's requests land: the served API beneath `service_url`.

    The one addressability check every caller needs before it sends a byte:
    `service_url` names a scheme and a host from `ADDRESSABLE_SCHEMES`, or
    nothing here is reachable.
    """

    address = urlsplit(service_url)
    if address.scheme not in ADDRESSABLE_SCHEMES or not address.netloc:
        raise AtelierApiAddressUnusable(
            f"{service_url!r} is not the address of a served Atelier API; "
            f"name one as {DEFAULT_SERVICE_URL!r}"
        )
    return service_url.rstrip("/") + API_PREFIX


class AtelierApi:
    """One `httpx.Client` bound to one served Atelier API base address.

    `transport` is a test seam only: production composition leaves it unset,
    so it reaches the real network. A caller owns the client's lifetime --
    use it as a context manager, or call `close()` once its invocation ends.
    """

    def __init__(
        self, service_url: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url = api_base_url(service_url)
        self._client = httpx.Client(transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    def get(
        self, path: str, *, accept: str = JSON_MEDIA_TYPE, timeout: float | None
    ) -> bytes:
        return self._request("GET", path, headers={"accept": accept}, timeout=timeout)

    def post(
        self,
        path: str,
        payload: bytes,
        *,
        media_type: str = JSON_MEDIA_TYPE,
        accept: str = JSON_MEDIA_TYPE,
        timeout: float | None,
    ) -> bytes:
        return self._request(
            "POST",
            path,
            content=payload,
            headers={"content-type": media_type, "accept": accept},
            timeout=timeout,
        )

    def event_lines(self, path: str, *, accept: str) -> Iterator[str]:
        """Every line of one server-sent-event answer, read as it arrives.

        The service ends this stream itself once the run it reports does, so
        it carries no read timeout of its own. Decoding is strict: a byte
        sequence this client cannot read as text is the same typed failure a
        non-2xx status is, never a silently substituted character.
        """

        url = self.base_url + path
        try:
            with self._client.stream(
                "GET", url, headers={"accept": accept}, timeout=None
            ) as response:
                self._raise_for_failure(response, url)
                yield from _decoded_lines(response.iter_bytes(), url)
        except httpx.HTTPError as unavailable:
            raise _transport_unavailable(url, unavailable) from None

    def bounded_get(
        self,
        path: str,
        *,
        accept: str = JSON_MEDIA_TYPE,
        read_timeout_seconds: float,
        maximum_bytes: int,
        into: BoundedRead,
    ) -> None:
        """One GET, read on a budget into `into` for a caller that does not yet
        trust the far side: identity-encoded (a compressed reply cannot outgrow
        the byte cap before the cap sees it) and the cap checked before a chunk
        is kept. The wall clock over this is the whole reading phase's one
        deadline (`reading_client`), never a deadline of this call's own.
        """

        url = self.base_url + path
        try:
            self._read_bounded(url, accept, read_timeout_seconds, maximum_bytes, into)
        except _ByteCapExceeded:
            raise AtelierApiTransportFailure(url, _BYTE_LIMIT_REASON) from None
        except httpx.HTTPError as unavailable:
            raise _transport_unavailable(url, unavailable) from None

    def sampled_event_frames(
        self,
        path: str,
        *,
        accept: str,
        read_timeout_seconds: float,
        maximum_bytes: int,
        maximum_frames: int,
        into: EventFrameCollection,
    ) -> None:
        """Read at most `maximum_frames` frames of one event stream into `into`.

        `event_lines` trusts the service to end the stream; this is for a
        caller that only wants to know whether a stream that may never end
        looks healthy now -- identity-encoded like `bounded_get`, its own read
        timeout on every chunk, and its byte cap checked before a chunk is
        kept. Each of those ends the sample rather than raising, because
        running into one is exactly what this call exists to survive; only a
        reply refused outright raises, and even then `into` still holds every
        frame already read.
        """

        url = self.base_url + path
        into.outcome = EventSampleOutcome.INTERRUPTED
        try:
            self._read_event_sample(
                url, accept, read_timeout_seconds, maximum_bytes, maximum_frames, into
            )
        except httpx.HTTPError as unavailable:
            raise _transport_unavailable(url, unavailable) from None

    def _read_bounded(
        self,
        url: str,
        accept: str,
        read_timeout_seconds: float,
        maximum_bytes: int,
        into: BoundedRead,
    ) -> None:
        with self._client.stream(
            "GET",
            url,
            headers={"accept": accept, **_IDENTITY_ENCODING_HEADERS},
            timeout=httpx.Timeout(read_timeout_seconds),
        ) as response:
            _refuse_unexpected_encoding(response, url)
            into.status = response.status_code
            for chunk in _capped(response.iter_bytes(), maximum_bytes):
                into.body.extend(chunk)

    def _read_event_sample(
        self,
        url: str,
        accept: str,
        read_timeout_seconds: float,
        maximum_bytes: int,
        maximum_frames: int,
        into: EventFrameCollection,
    ) -> None:
        with self._client.stream(
            "GET",
            url,
            headers={"accept": accept, **_IDENTITY_ENCODING_HEADERS},
            timeout=httpx.Timeout(read_timeout_seconds),
        ) as response:
            _refuse_unexpected_encoding(response, url)
            _refuse_unless_event_stream(response, url, maximum_bytes)
            _raise_for_bounded_failure(response, url, maximum_bytes)
            chunks = _capped(response.iter_bytes(), maximum_bytes, into)
            _collect_frames(_decoded_lines(chunks, url), maximum_frames, into)

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        timeout: float | None,
        content: bytes | None = None,
    ) -> bytes:
        url = self.base_url + path
        try:
            with self._client.stream(
                method, url, content=content, headers=headers, timeout=timeout
            ) as response:
                self._raise_for_failure(response, url)
                return response.read()
        except httpx.HTTPError as unavailable:
            raise _transport_unavailable(url, unavailable) from None

    @staticmethod
    def _raise_for_failure(response: httpx.Response, url: str) -> None:
        if response.is_success:
            return
        raise AtelierApiTransportFailure(
            url,
            response.reason_phrase,
            status=response.status_code,
            body=_bounded_body(response),
        )


@contextmanager
def opened_api(
    service_url: str, *, transport: httpx.BaseTransport | None = None
) -> Iterator[AtelierApi]:
    """One `AtelierApi` for a caller's whole invocation, closed when it ends.

    The one place every command builds and closes its own client: a caller
    already holding one -- an injected test double, or one it opened itself
    for a wider invocation -- never reaches this at all.
    """

    with AtelierApi(service_url, transport=transport) as api:
        yield api


class TransportFailureCategory(StrEnum):
    """What kind of transport trouble one call ran into, in this module's own
    fixed words.

    A library's exception message can carry the far side's own text, and its
    class alone loses the distinction that matters: one `ConnectError` covers
    a name that does not resolve, a certificate that does not verify, and a
    port that says no. This is the whole vocabulary a caller ever sees.
    """

    DNS = "dns"
    TLS = "tls"
    REFUSED = "refused"
    RESET = "reset"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    UNCLASSIFIED = "unclassified"


_CAUSES: Final[tuple[tuple[type[BaseException], TransportFailureCategory], ...]] = (
    (socket.gaierror, TransportFailureCategory.DNS),
    (ssl.SSLError, TransportFailureCategory.TLS),
    (ConnectionRefusedError, TransportFailureCategory.REFUSED),
    (ConnectionResetError, TransportFailureCategory.RESET),
    (BrokenPipeError, TransportFailureCategory.RESET),
)
"""What the operating system said underneath the wrappers httpx and httpcore
put around it -- the only place a name that does not resolve stays
distinguishable from a port that says no."""

_HTTPX_CLASSES: Final[
    tuple[tuple[type[httpx.HTTPError], TransportFailureCategory], ...]
] = (
    (httpx.TimeoutException, TransportFailureCategory.TIMEOUT),
    (httpx.ProtocolError, TransportFailureCategory.PROTOCOL),
    (httpx.UnsupportedProtocol, TransportFailureCategory.PROTOCOL),
    (httpx.DecodingError, TransportFailureCategory.PROTOCOL),
)
"""What httpx's own class says when nothing underneath it did."""


def _failure_category(error: httpx.HTTPError) -> TransportFailureCategory:
    """Which category `error` belongs to: what it was raised from first,
    because the operating system is more specific than the wrappers above it.

    Both links are followed. httpx chains its own wrapper explicitly
    (`__cause__`), while httpcore re-raises inside the original's handler and
    leaves only `__context__`, so a refused port would otherwise be
    indistinguishable from a name that does not resolve.
    """

    pending: deque[BaseException] = deque([error])
    seen: set[int] = set()
    while pending:
        raised = pending.popleft()
        if id(raised) in seen:
            continue
        seen.add(id(raised))
        for kind, category in _CAUSES:
            if isinstance(raised, kind):
                return category
        pending.extend(
            link for link in (raised.__cause__, raised.__context__) if link is not None
        )
    for kind, category in _HTTPX_CLASSES:
        if isinstance(error, kind):
            return category
    return TransportFailureCategory.UNCLASSIFIED


def _transport_unavailable(
    url: str, error: httpx.HTTPError
) -> AtelierApiTransportFailure:
    """`str(error)` is never used here, and every caller raises this `from
    None`: httpx's own transport exceptions can carry the far side's own text
    (a proxy's refusal line, for one), which a chained `__cause__` would carry
    straight into any traceback. Only a word from `TransportFailureCategory`
    -- this module's own vocabulary -- ever reaches this failure's reason.
    """

    return AtelierApiTransportFailure(
        url, f"transport failure: {_failure_category(error)}"
    )


def _bounded_body(response: httpx.Response) -> bytes:
    """Read at most `MAXIMUM_FAILURE_BODY_BYTES` of a failed answer's body.

    A caller only classifies a failure from this much; reading further
    would let an oversized or adversarial answer force unbounded memory for
    no gain a caller ever uses.
    """

    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAXIMUM_FAILURE_BODY_BYTES:
            break
    return bytes(body)


def _decoded_lines(chunks: Iterable[bytes], url: str) -> Iterator[str]:
    """Every line of a byte stream, decoded strictly as UTF-8 text.

    `httpx.Response.iter_lines` decodes with substitution, which would turn a
    malformed answer into a silently wrong one instead of a refusal; reading
    raw bytes and decoding them here keeps that failure loud.
    """

    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while (newline := buffer.find(b"\n")) != -1:
            yield _decoded_line(buffer[:newline], url)
            buffer = buffer[newline + 1 :]
    if buffer:
        yield _decoded_line(buffer, url)


def _collect_frames(
    lines: Iterator[str], maximum_frames: int, into: EventFrameCollection
) -> None:
    """Every frame `lines` assembles, up to `maximum_frames`, written into
    `into` as each one is decoded, and the outcome that ended the read.

    Frames land in the caller's own collection the moment they complete: an
    outer stop -- the reading phase's alarm firing mid-read -- still finds
    them there rather than losing what this call already knew.
    """

    try:
        for data in server_sent_data(lines):
            into.frames.append(data)
            if len(into.frames) >= maximum_frames:
                into.outcome = EventSampleOutcome.FRAME_LIMIT
                return
    except _ByteCapExceeded:
        into.outcome = EventSampleOutcome.BYTE_LIMIT
        return
    except httpx.ReadTimeout:
        into.outcome = EventSampleOutcome.SILENT
        return
    into.outcome = EventSampleOutcome.CLOSED_EARLY


def _raise_for_bounded_failure(
    response: httpx.Response, url: str, maximum_bytes: int
) -> None:
    """Like `AtelierApi._raise_for_failure`, but for a bounded caller's own
    read path: the refused body is read through the same byte-capped chunks a
    healthy one would be, never through the unbounded `_bounded_body` every
    other caller still reads through unchanged -- and, unlike
    `_raise_for_failure`, never `response.reason_phrase`: an HTTP reason
    phrase is text the far side wrote onto its own status line, not a value
    this module owns.
    """

    if response.is_success:
        return
    raise AtelierApiTransportFailure(
        url,
        "answered a non-2xx status without one of this API's own problem documents",
        status=response.status_code,
        body=_drained(_capped(response.iter_bytes(), maximum_bytes)),
    )


def _refuse_unexpected_encoding(response: httpx.Response, url: str) -> None:
    """Refuse a reply this call never asked to be given compressed, before
    reading a single byte of its body.

    `Accept-Encoding: identity` (`_IDENTITY_ENCODING_HEADERS`) is a request,
    not an enforcement: a reply that answers with a real `Content-Encoding`
    anyway would otherwise have that encoding decoded by `iter_bytes` itself,
    growing past this call's byte cap before the cap ever sees a byte of the
    result. The encoding value itself is never named back -- that it was
    refused is the whole diagnosis a caller needs.
    """

    encoding = response.headers.get("content-encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        raise AtelierApiTransportFailure(
            url, _UNEXPECTED_ENCODING_REASON, status=response.status_code
        )


def _refuse_unless_event_stream(
    response: httpx.Response, url: str, maximum_bytes: int
) -> None:
    """Decide from the content type alone, before one body byte is read,
    whether this reply is a stream a sample may frame.

    Draining an unknown body to find out what it was would keep the sample
    reading a reply it has already decided against: an error page that
    trickles a byte at a time trips no read timeout, so it would run out the
    whole reading phase and then look like a stream that merely sent no
    frame. The one body still read is this API's own problem document, whose
    body is what says which problem it is, under the same byte cap. A missing
    content type is refused with the rest -- `sse_starlette` publishes a real
    attention feed as exactly `text/event-stream`.
    """

    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if media_type.lower() == EVENT_STREAM_MEDIA_TYPE:
        return
    body = b""
    if media_type.lower() == PROBLEM_MEDIA_TYPE:
        body = _drained(_capped(response.iter_bytes(), maximum_bytes))
    raise AtelierApiTransportFailure(
        url, _NOT_EVENT_STREAM_REASON, status=response.status_code, body=body
    )


def _drained(chunks: Iterator[bytes]) -> bytes:
    """As much of `chunks` as this call's own bounds already allowed,
    collected for a caller that is about to refuse the reply and wants to
    classify why."""

    body = bytearray()
    try:
        for chunk in chunks:
            body.extend(chunk)
    except (_ByteCapExceeded, httpx.ReadTimeout):
        pass
    return bytes(body)


def _capped(
    chunks: Iterator[bytes],
    maximum_bytes: int,
    into: EventFrameCollection | None = None,
) -> Iterator[bytes]:
    """`chunks`, stopping before one would push their sum past a cap.

    The cap is checked before a chunk is ever added to a caller's own
    buffer, not after: what a caller never receives, it never has to hold,
    whatever that chunk's own size turns out to be. `into`, when given, is
    told how much has arrived after every chunk -- a caller that wants to
    know that even after an early stop keeps its own reference.
    """

    received = 0
    for chunk in chunks:
        if received + len(chunk) > maximum_bytes:
            raise _ByteCapExceeded
        received += len(chunk)
        if into is not None:
            into.bytes_read = received
        yield chunk


def server_sent_data(lines: Iterator[str]) -> Iterator[str]:
    """Every complete `data:` payload of an SSE stream, joined per frame.

    One frame's `data:` lines join with `\\n`, exactly as the SSE spec joins
    them; a blank line ends the frame. This reads only that generic framing,
    never a payload's own shape -- a caller decodes what the joined text
    means.
    """

    data_lines: list[str] = []
    for line in lines:
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
            data_lines = []
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data_lines.append(value.removeprefix(" "))
    if data_lines:
        yield "\n".join(data_lines)


def _decoded_line(raw_line: bytes, url: str) -> str:
    """Never `str(error)`, and never chained: `UnicodeDecodeError`'s own
    message names the specific byte value it failed on -- part of the
    answer's own content. Only the byte position -- a number -- says where,
    without saying what.
    """

    try:
        return raw_line.decode().rstrip("\r")
    except UnicodeDecodeError as error:
        position = error.start
        raise AtelierApiTransportFailure(
            url,
            f"the event stream carried bytes that are not UTF-8 text at "
            f"position {position}",
        ) from None
