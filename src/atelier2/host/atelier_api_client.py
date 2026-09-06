"""One `httpx`-backed client every host command speaks the Atelier API through.

`AtelierApi` validates the address a caller named, holds one `httpx.Client`
for the life of one invocation, and turns every transport-level problem --
a non-2xx answer, an unreachable service, or a stream that stops decoding as
text -- into one typed `AtelierApiTransportFailure`. A caller closes the
client when its invocation ends (a context manager, or `close()`), and
translates that one failure into whatever vocabulary its own callers expect.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Self
from urllib.parse import urlsplit

import httpx

from atelier2.api.openapi import API_PREFIX
from atelier2.host.address import ADDRESSABLE_SCHEMES, DEFAULT_SERVICE_URL

JSON_MEDIA_TYPE = "application/json"

MAXIMUM_FAILURE_BODY_BYTES = 4_096
"""How much of a non-2xx or malformed answer a caller reads before giving up
on classifying it further; past this, further bytes buy nothing but memory."""

AtelierApiTransport = httpx.BaseTransport
"""Re-exported so a caller's test seam names no second `httpx` import site."""


class AtelierApiAddressUnusable(ValueError):
    """`service_url` names nothing this client could ever reach."""


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
                yield from _decoded_lines(response, url)
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


def _decoded_lines(response: httpx.Response, url: str) -> Iterator[str]:
    """Every line of a byte stream, decoded strictly as UTF-8 text.

    `httpx.Response.iter_lines` decodes with substitution, which would turn a
    malformed answer into a silently wrong one instead of a refusal; reading
    raw bytes and decoding them here keeps that failure loud.
    """

    buffer = b""
    for chunk in response.iter_bytes():
        buffer += chunk
        while (newline := buffer.find(b"\n")) != -1:
            yield _decoded_line(buffer[:newline], url)
            buffer = buffer[newline + 1 :]
    if buffer:
        yield _decoded_line(buffer, url)


def _decoded_line(raw_line: bytes, url: str) -> str:
    try:
        return raw_line.decode().rstrip("\r")
    except UnicodeDecodeError as error:
        raise AtelierApiTransportFailure(
            url, f"the event stream carried bytes that are not UTF-8 text: {error}"
        ) from error
