"""One `httpx`-backed client every host command speaks the Atelier API through.

`AtelierApi` validates the address a caller named, holds one `httpx.Client`
for the life of one invocation, and turns every transport-level problem --
a non-2xx answer, an unreachable service, or a stream that stops decoding as
text -- into one typed `AtelierApiTransportFailure`. A caller closes the
client when its invocation ends (a context manager, or `close()`), and
translates that one failure into whatever vocabulary its own callers expect.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
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


class AtelierApiAddressUnusable(ValueError):
    """`service_url` names nothing this client could ever reach."""


class EventSampleLimit(StrEnum):
    """Why `AtelierApi.sampled_event_frames` stopped itself before the stream did.

    A stream this call samples may run forever by design, so every dimension
    it could hang or grow on stops the read itself instead of the connection
    ending on its own: `SILENT` is a read-timeout wait with no byte arriving,
    `FRAME_LIMIT` and `BYTE_LIMIT` are its own caps, and `OVERALL_DEADLINE` is
    the whole call's wall-clock budget, checked before every read this call
    makes -- the request itself, every chunk, and an error body alike -- not
    only between whole frames. The connection ending on its own within budget
    is not one of these -- a caller reads that from `BoundedEventSample.stopped`
    being `None`.
    """

    SILENT = "silent"
    FRAME_LIMIT = "frame-limit"
    BYTE_LIMIT = "byte-limit"
    OVERALL_DEADLINE = "overall-deadline"


@dataclass(frozen=True, slots=True)
class BoundedEventSample:
    """As much of one event stream as `sampled_event_frames` read before stopping.

    `frames` is every complete `data:` payload it decoded, oldest first.
    `stopped` names why the read stopped itself; it is `None` when the
    connection closed on its own -- an early end for a stream documented to
    never end, and exactly as reportable as any of the named limits.
    `bytes_read` is every byte this call actually received, whether or not
    any of them assembled into a complete frame -- a caller that reads
    `frames == ()` still needs this to tell a truly silent connection from
    one that spoke (a heartbeat comment, say) and then closed.
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


class _BoundedReadStopped(Exception):
    """Raised inside a bounded read's own chunk generator; never escapes this
    module -- each caller catches it and reports in its own vocabulary."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _ByteCounter:
    """How many bytes a bounded read has actually received so far.

    Mutable and handed to `_capped` by reference: a caller keeps its own
    reference and reads `.total` once reading stops, for whatever reason --
    `_capped` itself only ever grows it, never reports it back."""

    total: int = 0


_EVENT_SAMPLE_STOP_REASONS: Final[dict[str, EventSampleLimit]] = {
    "overall deadline": EventSampleLimit.OVERALL_DEADLINE,
    "byte limit": EventSampleLimit.BYTE_LIMIT,
}


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
            raise _transport_unavailable(url, unavailable) from unavailable

    def bounded_get(
        self,
        path: str,
        *,
        accept: str = JSON_MEDIA_TYPE,
        request_timeout_seconds: float,
        overall_deadline_seconds: float,
        maximum_bytes: int,
    ) -> BoundedResponse:
        """One GET, read on a budget for a caller that does not yet trust the
        far side: identity-encoded, so a compressed reply cannot outgrow this
        call's byte cap between the wire and the buffer before that cap is
        ever checked; the cap itself is checked before a chunk is kept, never
        after; and every read this call makes -- the request itself, then
        every chunk -- happens only once one wall-clock deadline still
        allows it, on top of the read timeout each still carries on its own.
        """

        url = self.base_url + path
        deadline = time.monotonic() + overall_deadline_seconds
        try:
            if time.monotonic() >= deadline:
                raise _BoundedReadStopped("overall deadline")
            with self._client.stream(
                "GET",
                url,
                headers={"accept": accept, **_IDENTITY_ENCODING_HEADERS},
                timeout=httpx.Timeout(request_timeout_seconds),
            ) as response:
                _refuse_unexpected_encoding(response, url)
                body = bytearray()
                for chunk in _capped(_paced_chunks(response, deadline), maximum_bytes):
                    body.extend(chunk)
                return BoundedResponse(status=response.status_code, body=bytes(body))
        except _BoundedReadStopped as stopped:
            raise AtelierApiTransportFailure(
                url, f"a bounded read gave up: {stopped.reason}"
            ) from stopped
        except httpx.HTTPError as unavailable:
            raise _transport_unavailable(url, unavailable) from unavailable

    def sampled_event_frames(
        self,
        path: str,
        *,
        accept: str,
        request_timeout_seconds: float,
        overall_deadline_seconds: float,
        maximum_bytes: int,
        maximum_frames: int,
    ) -> BoundedEventSample:
        """Read at most `maximum_frames` frames of one event stream, on a budget.

        `event_lines` trusts the service to end the stream; this is for a
        caller that only wants to know whether a stream that may never end on
        its own looks healthy right now, identity-encoded for the same reason
        `bounded_get` is. `request_timeout_seconds` is the read timeout on
        every chunk (a silent stream stops this call rather than hanging it);
        `overall_deadline_seconds` is checked before every read this call
        makes -- the request itself, every chunk of a healthy body or of a
        refused one -- not only between whole frames, so a reply that is all
        comments or all partial frames cannot outrun it either; and
        `maximum_bytes` bounds what it buffers, checked before a chunk is
        kept, never after. Each is named in the returned sample -- see
        `_collected_sample` -- rather than a raised failure, because running
        into one of them is exactly what this call exists to survive. It
        always closes the connection itself before returning.
        """

        url = self.base_url + path
        deadline = time.monotonic() + overall_deadline_seconds
        try:
            if time.monotonic() >= deadline:
                return BoundedEventSample(
                    frames=(), stopped=EventSampleLimit.OVERALL_DEADLINE, bytes_read=0
                )
            with self._client.stream(
                "GET",
                url,
                headers={"accept": accept, **_IDENTITY_ENCODING_HEADERS},
                timeout=httpx.Timeout(request_timeout_seconds),
            ) as response:
                _refuse_unexpected_encoding(response, url)
                _raise_for_bounded_failure(response, url, deadline, maximum_bytes)
                _raise_if_not_event_stream(response, url, deadline, maximum_bytes)
                counter = _ByteCounter()
                lines = _decoded_lines(
                    _capped(_paced_chunks(response, deadline), maximum_bytes, counter),
                    url,
                )
                return _collected_sample(lines, maximum_frames, counter)
        except httpx.HTTPError as unavailable:
            raise _transport_unavailable(url, unavailable) from unavailable

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
            raise _transport_unavailable(url, unavailable) from unavailable

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
    return AtelierApiTransportFailure(url, str(error))


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
    lines: Iterator[str], maximum_frames: int, counter: _ByteCounter
) -> BoundedEventSample:
    """Every frame `lines` assembles, up to `maximum_frames`, and why the
    read stopped before that or before the stream itself ended: its own
    limit, or whichever named reason the bounded chunks beneath `lines`
    raised."""

    frames: list[str] = []
    stopped: EventSampleLimit | None = None
    try:
        for data in server_sent_data(lines):
            frames.append(data)
            if len(frames) >= maximum_frames:
                stopped = EventSampleLimit.FRAME_LIMIT
                break
    except _BoundedReadStopped as stopped_read:
        stopped = _EVENT_SAMPLE_STOP_REASONS[stopped_read.reason]
    except httpx.ReadTimeout:
        stopped = EventSampleLimit.SILENT
    return BoundedEventSample(
        frames=tuple(frames), stopped=stopped, bytes_read=counter.total
    )


def _raise_for_bounded_failure(
    response: httpx.Response, url: str, deadline: float, maximum_bytes: int
) -> None:
    """Like `AtelierApi._raise_for_failure`, but for a bounded caller's own
    read path: the refused body is read through the same deadline- and
    byte-capped chunks a healthy one would be, never through the unbounded
    `_bounded_body` every other caller still reads through unchanged.
    """

    if response.is_success:
        return
    raise AtelierApiTransportFailure(
        url,
        response.reason_phrase,
        status=response.status_code,
        body=_drained(response, deadline, maximum_bytes),
    )


def _refuse_unexpected_encoding(response: httpx.Response, url: str) -> None:
    """Refuse a reply this call never asked to be given compressed, before
    reading a single byte of its body.

    `Accept-Encoding: identity` (`_IDENTITY_ENCODING_HEADERS`) is a request,
    not an enforcement: a reply that answers with a real `Content-Encoding`
    anyway would otherwise have that encoding decoded by `iter_bytes` itself,
    growing past this call's byte cap before the cap ever sees a byte of the
    result. Headers are already in hand at this point -- nothing has read
    the body yet.
    """

    encoding = response.headers.get("content-encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        raise AtelierApiTransportFailure(
            url,
            f"answered content-encoding {encoding!r}, refused before reading its body",
            status=response.status_code,
        )


def _raise_if_not_event_stream(
    response: httpx.Response, url: str, deadline: float, maximum_bytes: int
) -> None:
    """Refuse a reply that does not carry this feed's own content type,
    before this call's SSE framing ever tries to read it as one.

    A route caught before it ever starts streaming -- an exception handler,
    a proxy's own error page -- answers its own content type, most often a
    bare problem document; `server_sent_data` only ever reads `data:` lines
    and has no opinion about anything else, so a body like that would
    otherwise vanish without a single finding. `sse_starlette` publishes
    every real attention-feed answer as exactly `text/event-stream`
    (its own default `media_type`), so that content type is trusted here
    the same way a status code is -- and its absence is not: an empty or
    missing `Content-Type` is not this call's evidence of anything, so it
    passes through to the normal per-frame read unread.
    """

    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if not media_type or media_type.lower() == _EVENT_STREAM_CONTENT_TYPE:
        return
    raise AtelierApiTransportFailure(
        url,
        f"answered content-type {media_type!r}, not an event stream",
        status=response.status_code,
        body=_drained(response, deadline, maximum_bytes),
    )


def _drained(response: httpx.Response, deadline: float, maximum_bytes: int) -> bytes:
    """As much of `response`'s body as this call's own bounds allow, read
    for a caller that is about to refuse it and wants to classify why."""

    body = bytearray()
    try:
        for chunk in _capped(_paced_chunks(response, deadline), maximum_bytes):
            body.extend(chunk)
    except (_BoundedReadStopped, httpx.ReadTimeout):
        pass
    return bytes(body)


def _paced_chunks(response: httpx.Response, deadline: float) -> Iterator[bytes]:
    """This response's own chunks, one at a time, each requested only once
    the wall-clock deadline still allows it.

    The check runs before every read this makes -- including the very first,
    before this response has even answered -- not only between whole frames a
    higher layer assembles from them, so a reply that never completes one (all
    comments, or one endless partial line) cannot outrun it either.

    This does not ask `iter_bytes` for a bounded `chunk_size`: httpx buffers
    ahead to fill one before it ever yields it, so a reply that goes silent
    or fails mid-read loses everything already received instead of handing
    it back -- worse than the unbounded read this replaces, not safer. What
    already arrived over the wire is bounded by the transport itself; the
    identity encoding this call's caller asks for is what keeps one transport
    chunk from ever decoding into something larger than it was on the wire.
    """

    iterator = response.iter_bytes()
    while True:
        if time.monotonic() >= deadline:
            raise _BoundedReadStopped("overall deadline")
        try:
            yield next(iterator)
        except StopIteration:
            return


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
            raise _BoundedReadStopped("byte limit")
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
    try:
        return raw_line.decode().rstrip("\r")
    except UnicodeDecodeError as error:
        raise AtelierApiTransportFailure(
            url, f"the event stream carried bytes that are not UTF-8 text: {error}"
        ) from error
