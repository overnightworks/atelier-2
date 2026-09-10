"""One `httpx`-backed client every host command speaks the Atelier API through.

`AtelierApi` validates the address a caller named, holds one `httpx.Client`
for the life of one invocation, and turns every transport-level problem --
a non-2xx answer, an unreachable service, or a stream that stops decoding as
text -- into one typed `AtelierApiTransportFailure`. A caller closes the
client when its invocation ends (a context manager, or `close()`), and
translates that one failure into whatever vocabulary its own callers expect.

No text an answer wrote ever reaches a failure raised here: not a reason
phrase, not a served header value, and not a library exception's own message
-- every translation is `from None`, so a traceback cannot carry one out
either.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import FrameType
from typing import Final, Self
from urllib.parse import urlsplit

import httpx

from atelier2.api.openapi import API_PREFIX
from atelier2.host.address import ADDRESSABLE_SCHEMES, DEFAULT_SERVICE_URL

JSON_MEDIA_TYPE = "application/json"

MAXIMUM_FAILURE_BODY_BYTES = 4_096
"""How much of a non-2xx or malformed answer a caller reads before giving up
on classifying it further; past this, further bytes buy nothing but memory."""

_IDENTITY_ENCODING_HEADERS: Final = {"accept-encoding": "identity"}
"""A bounded read asks for the wire bytes as they are: a compressed reply
could expand past this call's byte cap between the wire and the buffer,
after the cap already thought it was safe."""

_EVENT_STREAM_CONTENT_TYPE: Final = "text/event-stream"

_UNEXPECTED_ENCODING_REASON: Final = (
    "answered with a content-encoding other than identity, refused before "
    "reading its body"
)

_NOT_EVENT_STREAM_REASON: Final = (
    "answered something that does not look like an event stream, refused "
    "before reading it as one"
)

_BYTE_LIMIT_REASON: Final = "a bounded read gave up: byte limit"
_DEADLINE_REASON: Final = "a bounded read gave up: overall deadline"


class AtelierApiAddressUnusable(ValueError):
    """`service_url` names nothing this client could ever reach."""


class WallClockDeadlineExceeded(Exception):
    """One bounded call's absolute deadline ran out while it was still reading.

    Raised on the calling thread by the deadline's own alarm, so it interrupts
    whichever blocking read that thread sits in -- headers that never
    complete, a socket that never speaks again -- and unwinds through the
    `with` blocks that close the response and its connection.
    """


class WallClockDeadlineUnavailable(RuntimeError):
    """A bounded call asked for a deadline this thread cannot be given.

    Only the main thread receives the process's alarm, so anywhere else this
    refuses rather than running an unbounded read that merely looks bounded.
    """


@contextmanager
def wall_clock_deadline(seconds: float) -> Iterator[None]:
    """Stop whatever the calling thread is doing once `seconds` have passed.

    A read timeout bounds one read; this bounds the whole block, including a
    reply whose headers never finish arriving and a read that never returns
    at all. The alarm is disarmed before its handler is restored: a signal
    already delivered as the block ends then resolves against the restored
    handler and does nothing, instead of landing inside an unrelated later
    call.
    """

    if threading.current_thread() is not threading.main_thread():
        raise WallClockDeadlineUnavailable(
            "a bounded read's deadline is enforced by this process's alarm, "
            "which only the main thread receives"
        )
    if seconds <= 0:
        raise WallClockDeadlineExceeded

    def expire(signal_number: int, frame: FrameType | None) -> None:
        del signal_number, frame
        raise WallClockDeadlineExceeded

    previous_handler = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


class EventSampleLimit(StrEnum):
    """Why `AtelierApi.sampled_event_frames` stopped itself before the stream did.

    A stream this call samples may run forever by design, so every dimension
    it could hang or grow on stops the read itself: `SILENT` is a read-timeout
    wait with no byte arriving, `FRAME_LIMIT` and `BYTE_LIMIT` are its own
    caps, and `OVERALL_DEADLINE` is the whole call's enforced wall clock. The
    connection ending on its own within budget is none of these -- a caller
    reads that from `BoundedEventSample.stopped` being `None`.
    """

    SILENT = "silent"
    FRAME_LIMIT = "frame-limit"
    BYTE_LIMIT = "byte-limit"
    OVERALL_DEADLINE = "overall-deadline"


@dataclass(frozen=True, slots=True)
class BoundedEventSample:
    """As much of one event stream as `sampled_event_frames` read before stopping.

    `frames` is every complete `data:` payload it decoded, oldest first --
    including the ones already in hand when the deadline cut the read short,
    so a failure frame this call did read is never lost to the stop that
    followed it. `stopped` names why the read stopped itself; it is `None`
    when the connection closed on its own -- an early end for a stream
    documented to never end. `bytes_read` is every byte this call received,
    whether or not any of them assembled into a complete frame.
    """

    frames: tuple[str, ...]
    stopped: EventSampleLimit | None
    bytes_read: int


@dataclass(frozen=True, slots=True)
class BoundedResponse:
    """One `bounded_get` answer: its status and its capped body, together.

    Unlike `get`, a non-2xx status is not raised here -- a caller reading an
    instance it does not yet trust classifies the body itself, whichever
    status carried it. Only a transport-level failure -- unreachable, timed
    out, or over this call's own budget -- is still `AtelierApiTransportFailure`.
    """

    status: int
    body: bytes


class _ByteCapExceeded(Exception):
    """Raised inside a bounded read's own chunk generator once the next chunk
    would push it past its cap; never escapes this module."""


@dataclass
class _ByteCounter:
    """How many bytes a bounded read has actually received so far.

    Mutable and handed to `_capped` by reference: a caller keeps its own
    reference and reads `.total` once reading stops, for whatever reason."""

    total: int = 0


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
        deadline_seconds: float,
        maximum_bytes: int,
    ) -> BoundedResponse:
        """One GET, read on a budget for a caller that does not yet trust the
        far side: identity-encoded (a compressed reply cannot outgrow the byte
        cap before the cap sees it), the cap checked before a chunk is kept,
        and the whole call under `wall_clock_deadline` -- so a reply that
        never finishes arriving ends this call rather than outliving it.
        """

        url = self.base_url + path
        try:
            with wall_clock_deadline(deadline_seconds):
                return self._read_bounded(
                    url, accept, read_timeout_seconds, maximum_bytes
                )
        except WallClockDeadlineExceeded:
            raise AtelierApiTransportFailure(url, _DEADLINE_REASON) from None
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
        deadline_seconds: float,
        maximum_bytes: int,
        maximum_frames: int,
    ) -> BoundedEventSample:
        """Read at most `maximum_frames` frames of one event stream, on a budget.

        `event_lines` trusts the service to end the stream; this is for a
        caller that only wants to know whether a stream that may never end
        looks healthy now -- identity-encoded like `bounded_get`, its own read
        timeout on every chunk, its byte cap checked before a chunk is kept,
        and the whole call under `wall_clock_deadline`. Each of those is named
        in the returned sample rather than raised, because running into one is
        exactly what this call exists to survive, and the frames already read
        come back with it.
        """

        url = self.base_url + path
        collected: list[str] = []
        counter = _ByteCounter()
        try:
            with wall_clock_deadline(deadline_seconds):
                return self._read_event_sample(
                    url,
                    accept,
                    read_timeout_seconds,
                    maximum_bytes,
                    maximum_frames,
                    collected,
                    counter,
                )
        except WallClockDeadlineExceeded:
            return BoundedEventSample(
                frames=tuple(collected),
                stopped=EventSampleLimit.OVERALL_DEADLINE,
                bytes_read=counter.total,
            )
        except httpx.HTTPError as unavailable:
            raise _transport_unavailable(url, unavailable) from None

    def _read_bounded(
        self,
        url: str,
        accept: str,
        read_timeout_seconds: float,
        maximum_bytes: int,
    ) -> BoundedResponse:
        with self._client.stream(
            "GET",
            url,
            headers={"accept": accept, **_IDENTITY_ENCODING_HEADERS},
            timeout=httpx.Timeout(read_timeout_seconds),
        ) as response:
            _refuse_unexpected_encoding(response, url)
            body = bytearray()
            for chunk in _capped(response.iter_bytes(), maximum_bytes):
                body.extend(chunk)
            return BoundedResponse(status=response.status_code, body=bytes(body))

    def _read_event_sample(
        self,
        url: str,
        accept: str,
        read_timeout_seconds: float,
        maximum_bytes: int,
        maximum_frames: int,
        collected: list[str],
        counter: _ByteCounter,
    ) -> BoundedEventSample:
        with self._client.stream(
            "GET",
            url,
            headers={"accept": accept, **_IDENTITY_ENCODING_HEADERS},
            timeout=httpx.Timeout(read_timeout_seconds),
        ) as response:
            _refuse_unexpected_encoding(response, url)
            _raise_for_bounded_failure(response, url, maximum_bytes)
            chunks = _capped(response.iter_bytes(), maximum_bytes, counter)
            try:
                chunks = _raise_if_not_event_stream(response, url, chunks)
            except _ByteCapExceeded:
                return BoundedEventSample(
                    frames=(),
                    stopped=EventSampleLimit.BYTE_LIMIT,
                    bytes_read=counter.total,
                )
            except httpx.ReadTimeout:
                return BoundedEventSample(
                    frames=(), stopped=EventSampleLimit.SILENT, bytes_read=counter.total
                )
            lines = _decoded_lines(chunks, url)
            return _collected_sample(lines, maximum_frames, collected, counter)

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


def _transport_unavailable(
    url: str, error: httpx.HTTPError
) -> AtelierApiTransportFailure:
    """`str(error)` is never used here, and every caller raises this `from
    None`: httpx's own transport exceptions can carry the far side's own text
    (a proxy's refusal line, for one), which a chained `__cause__` would carry
    straight into any traceback. Only the exception's class name -- one of
    httpx's own fixed, known set -- ever reaches this failure's reason.
    """

    return AtelierApiTransportFailure(url, f"transport failure: {type(error).__name__}")


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


def _collected_sample(
    lines: Iterator[str],
    maximum_frames: int,
    collected: list[str],
    counter: _ByteCounter,
) -> BoundedEventSample:
    """Every frame `lines` assembles, up to `maximum_frames`, and why the read
    stopped before that or before the stream itself ended.

    Frames land in `collected`, the caller's own list, as they are decoded:
    an outer stop -- the deadline's alarm firing mid-read -- still finds them
    there rather than losing what this call already knows.
    """

    stopped: EventSampleLimit | None = None
    try:
        for data in server_sent_data(lines):
            collected.append(data)
            if len(collected) >= maximum_frames:
                stopped = EventSampleLimit.FRAME_LIMIT
                break
    except _ByteCapExceeded:
        stopped = EventSampleLimit.BYTE_LIMIT
    except httpx.ReadTimeout:
        stopped = EventSampleLimit.SILENT
    return BoundedEventSample(
        frames=tuple(collected), stopped=stopped, bytes_read=counter.total
    )


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


def _raise_if_not_event_stream(
    response: httpx.Response, url: str, chunks: Iterator[bytes]
) -> Iterator[bytes]:
    """Refuse a reply that is not an event stream, before this call's SSE
    framing ever tries to read it as one -- and hand back an iterator that
    still yields everything `chunks` would have, for a caller that reads on.

    A route caught before it ever starts streaming answers its own content
    type and, most often, a bare problem document `server_sent_data` (which
    only reads `data:` lines) would otherwise let vanish unreported.
    `sse_starlette` always publishes a real attention feed as exactly
    `text/event-stream`, so a *present* content type is trusted the same
    way a status code is; a missing one is not passed through unread
    (a stripped header on a real stream could abuse that) -- this peeks the
    first chunk instead and refuses one opening with `{`, which an event
    stream's own framing (`data:`, `event:`, `id:`, a `:` comment, blank)
    never does.
    """

    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if media_type:
        if media_type.lower() == _EVENT_STREAM_CONTENT_TYPE:
            return chunks
        raise AtelierApiTransportFailure(
            url,
            _NOT_EVENT_STREAM_REASON,
            status=response.status_code,
            body=_drained(chunks),
        )
    first, chunks = _peeked_first(chunks)
    if first is not None and first.lstrip().startswith(b"{"):
        raise AtelierApiTransportFailure(
            url,
            _NOT_EVENT_STREAM_REASON,
            status=response.status_code,
            body=_drained(chunks),
        )
    return chunks


def _peeked_first(chunks: Iterator[bytes]) -> tuple[bytes | None, Iterator[bytes]]:
    """The first chunk `chunks` would yield, and an iterator that still
    yields everything `chunks` would have, first chunk included -- letting a
    caller inspect a stream's opening bytes without losing them for
    whichever reader consumes the rest.
    """

    try:
        first = next(chunks)
    except StopIteration:
        return None, iter(())

    def replayed() -> Iterator[bytes]:
        yield first
        yield from chunks

    return first, replayed()


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
    counter: _ByteCounter | None = None,
) -> Iterator[bytes]:
    """`chunks`, stopping before one would push their sum past a cap.

    The cap is checked before a chunk is ever added to a caller's own
    buffer, not after: what a caller never receives, it never has to hold,
    whatever that chunk's own size turns out to be. `counter`, when given,
    is grown by every chunk this actually yields -- a caller that wants to
    know how much arrived even after an early stop keeps its own reference.
    """

    counted = counter if counter is not None else _ByteCounter()
    for chunk in chunks:
        if counted.total + len(chunk) > maximum_bytes:
            raise _ByteCapExceeded
        counted.total += len(chunk)
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
