"""The one HTTP client every host command builds its own refusal wording on.

This module pins the owner's own contract -- address validation, a
successful call's bytes, and how a transport failure is typed -- so a host
command's own tests never need to fake a socket to prove this part again.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from atelier2.host.address import DEFAULT_SERVICE_URL
from atelier2.host.atelier_api_client import (
    AtelierApi,
    AtelierApiAddressUnusable,
    AtelierApiTransportFailure,
    api_base_url,
)

SERVICE_URL = "http://127.0.0.1:8422"


def test_a_served_address_gains_the_published_api_prefix() -> None:
    assert api_base_url(SERVICE_URL) == f"{SERVICE_URL}/atelier/api/v1"


def test_a_trailing_slash_does_not_double_the_prefix() -> None:
    assert api_base_url(f"{SERVICE_URL}/") == f"{SERVICE_URL}/atelier/api/v1"


@pytest.mark.parametrize("service_url", ["not-a-url", "ftp://127.0.0.1:8422", ""])
def test_an_unaddressable_service_url_is_refused_by_name(service_url: str) -> None:
    with pytest.raises(AtelierApiAddressUnusable) as refusal:
        api_base_url(service_url)

    assert repr(service_url) in str(refusal.value)
    assert DEFAULT_SERVICE_URL in str(refusal.value)


def test_an_unaddressable_service_url_refuses_client_construction() -> None:
    with pytest.raises(AtelierApiAddressUnusable):
        AtelierApi("not-a-url")


def _echoing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"method": request.method, "path": request.url.path}
    )


def test_get_and_post_reach_the_named_path_beneath_the_service() -> None:
    api = AtelierApi(SERVICE_URL, transport=httpx.MockTransport(_echoing))

    get_answer = api.get("/health", timeout=1.0)
    post_answer = api.post("/runs", b"{}", timeout=1.0)

    assert b"/atelier/api/v1/health" in get_answer
    assert b"/atelier/api/v1/runs" in post_answer
    assert b'"POST"' in post_answer


def _refusing(status: int, body: bytes) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handle)


def test_a_non_2xx_answer_carries_its_status_and_exact_body() -> None:
    api = AtelierApi(SERVICE_URL, transport=_refusing(404, b'{"detail":"gone"}'))

    with pytest.raises(AtelierApiTransportFailure) as failure:
        api.get("/runs/missing", timeout=1.0)

    assert failure.value.status == 404
    assert failure.value.body == b'{"detail":"gone"}'
    assert failure.value.url.endswith("/runs/missing")


def test_an_unreachable_service_carries_no_status() -> None:
    def refuse_to_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    api = AtelierApi(SERVICE_URL, transport=httpx.MockTransport(refuse_to_connect))

    with pytest.raises(AtelierApiTransportFailure) as failure:
        api.get("/health", timeout=1.0)

    assert failure.value.status is None
    assert failure.value.body == b""


def _streaming(*frames: str) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content="".join(frames).encode())

    return httpx.MockTransport(handle)


def test_event_lines_yields_every_line_of_a_successful_stream() -> None:
    api = AtelierApi(
        SERVICE_URL, transport=_streaming("data: one\n\n", "data: two\n\n")
    )

    lines: Iterator[str] = api.event_lines(
        "/runs/r1/events", accept="text/event-stream"
    )

    assert list(lines) == ["data: one", "", "data: two", ""]


def test_event_lines_raises_the_transport_failure_before_yielding_a_refused_stream() -> (
    None
):
    api = AtelierApi(SERVICE_URL, transport=_refusing(404, b"not found"))

    with pytest.raises(AtelierApiTransportFailure) as failure:
        list(api.event_lines("/runs/missing/events", accept="text/event-stream"))

    assert failure.value.status == 404
