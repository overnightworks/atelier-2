"""The one HTTP client every host command uses to speak the served Atelier API.

Three doors reach the same public HTTP surface -- `run`/`resolve`, the stdio
MCP child, and the provider canary -- and until now each carried its own
`urllib` code to get there: its own address check, its own request execution,
its own mapping of a non-2xx answer or an unreachable socket into something
typed. This module is the one owner of that mechanics: it validates the
address a caller named, sends the request through `httpx`, and turns every
transport-level failure into one typed refusal.

Decoding a validated resource, and the vocabulary a refusal speaks in, stay
with the door that already owns them (`run`'s `RunCommandRefusal` family, the
provider canary's own `ProviderCanary*` names): each keeps its own wording
because its tests pin it, and each carries its own answer's decode failure
into its own existing exception. This module raises only the one transport
refusal below; it names no caller's vocabulary, on pain of the very import
cycle it exists to cut.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx

from atelier2.api.openapi import API_PREFIX
from atelier2.host.address import ADDRESSABLE_SCHEMES, DEFAULT_SERVICE_URL

JSON_MEDIA_TYPE = "application/json"

AtelierApiTransport = httpx.BaseTransport
"""Re-exported so a caller's test seam names no second `httpx` import site."""


class AtelierApiAddressUnusable(ValueError):
    """`service_url` names nothing this client could ever reach."""


class AtelierApiTransportFailure(Exception):
    """One HTTP call to the Atelier API did not answer as this client asked.

    `status` is `None` for a connection or timeout failure that reached no
    response at all -- `reason` then names the transport's own complaint --
    and otherwise the non-2xx status the service did answer with, `reason`
    its bare HTTP reason phrase and `body` its exact response bytes.
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
    so it reaches the real network.
    """

    def __init__(
        self, service_url: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url = api_base_url(service_url)
        self._client = httpx.Client(transport=transport)

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
        it carries no read timeout of its own.
        """

        url = self.base_url + path
        try:
            with self._client.stream(
                "GET", url, headers={"accept": accept}, timeout=None
            ) as response:
                self._raise_for_failure(response, url)
                yield from response.iter_lines()
        except httpx.HTTPError as unavailable:
            raise AtelierApiTransportFailure(url, str(unavailable)) from unavailable

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
            response = self._client.request(
                method, url, content=content, headers=headers, timeout=timeout
            )
        except httpx.HTTPError as unavailable:
            raise AtelierApiTransportFailure(url, str(unavailable)) from unavailable
        self._raise_for_failure(response, url)
        return response.content

    @staticmethod
    def _raise_for_failure(response: httpx.Response, url: str) -> None:
        if response.is_success:
            return
        raise AtelierApiTransportFailure(
            url,
            response.reason_phrase,
            status=response.status_code,
            body=response.read(),
        )
