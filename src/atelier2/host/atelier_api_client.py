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
from typing import Self
from urllib.parse import urlsplit

import httpx

from atelier2.api.openapi import API_PREFIX
from atelier2.host.address import ADDRESSABLE_SCHEMES, DEFAULT_SERVICE_URL

JSON_MEDIA_TYPE = "application/json"

MAXIMUM_FAILURE_BODY_BYTES = 4_096
"""How much of a non-2xx or malformed answer a caller reads before giving up
on classifying it further; past this, further bytes buy nothing but memory."""


class AtelierApiAddressUnusable(ValueError):
    """`service_url` names nothing this client could ever reach."""


class EventSampleLimit(StrEnum):
    """Why `AtelierApi.sampled_event_frames` stopped itself before the stream did.

    A stream this call samples may run forever by design, so every dimension
    it could hang or grow on stops the read itself instead of the connection
    ending on its own: `SILENT` is a request-timeout wait with no byte
    arriving, `FRAME_LIMIT` and `BYTE_LIMIT` are its own caps, and
    `OVERALL_DEADLINE` is the whole call's wall-clock budget. The connection
    ending on its own within budget is not one of these -- a caller reads
    that from `BoundedEventSample.stopped` being `None`.
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
    """

    frames: tuple[str, ...]
    stopped: EventSampleLimit | None


class _EventSampleBudgetExceeded(Exception):
    def __init__(self, limit: EventSampleLimit) -> None:
        super().__init__(limit.value)
        self.limit = limit


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
        its own looks healthy right now. `request_timeout_seconds` is the read
        timeout on every chunk (a silent stream stops this call rather than
        hanging it), `overall_deadline_seconds` bounds the whole call's
        wall-clock time, and `maximum_bytes` bounds what it buffers -- each is
        named in the returned sample, never a raised failure, because running
        into one of them is exactly what this call exists to survive. It
        always closes the connection itself before returning.
        """

        url = self.base_url + path
        deadline = time.monotonic() + overall_deadline_seconds
        frames: list[str] = []
        stopped: EventSampleLimit | None = None
        try:
            with self._client.stream(
                "GET",
                url,
                headers={"accept": accept},
                timeout=httpx.Timeout(request_timeout_seconds),
            ) as response:
                self._raise_for_failure(response, url)
                lines = _decoded_lines(_bounded_chunks(response, maximum_bytes), url)
                try:
                    for data in server_sent_data(lines):
                        frames.append(data)
                        if len(frames) >= maximum_frames:
                            stopped = EventSampleLimit.FRAME_LIMIT
                            break
                        if time.monotonic() >= deadline:
                            stopped = EventSampleLimit.OVERALL_DEADLINE
                            break
                except _EventSampleBudgetExceeded as budget:
                    stopped = budget.limit
                except httpx.ReadTimeout:
                    stopped = EventSampleLimit.SILENT
        except httpx.HTTPError as unavailable:
            raise _transport_unavailable(url, unavailable) from unavailable
        return BoundedEventSample(frames=tuple(frames), stopped=stopped)

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


def _bounded_chunks(response: httpx.Response, maximum_bytes: int) -> Iterator[bytes]:
    """A response's byte chunks, stopping the instant their sum passes a cap.

    `maximum_bytes` bounds what `_decoded_lines` ever buffers from this
    response: raising past it, rather than yielding a truncated chunk, keeps
    the byte cap and the frame-limit/deadline caps in `sampled_event_frames`
    reported through the same one path.
    """

    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > maximum_bytes:
            raise _EventSampleBudgetExceeded(EventSampleLimit.BYTE_LIMIT)
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
