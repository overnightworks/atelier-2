"""The one HTTP client every host command builds its own refusal wording on.

This module pins the owner's own contract -- address validation, a
successful call's bytes, how a transport failure is typed, and how a stream
or an oversized failure body is bounded -- so a host command's own tests
never need to fake a socket to prove this part again.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from atelier2.api.openapi import API_PREFIX
from atelier2.host.address import DEFAULT_SERVICE_URL
from atelier2.host.atelier_api_client import (
    MAXIMUM_FAILURE_BODY_BYTES,
    AtelierApi,
    AtelierApiAddressUnusable,
    AtelierApiTransportFailure,
    api_base_url,
)

SERVICE_URL = "http://127.0.0.1:8422"


def test_a_served_address_gains_the_published_api_prefix() -> None:
    assert api_base_url(SERVICE_URL) == f"{SERVICE_URL}{API_PREFIX}"


def test_a_trailing_slash_does_not_double_the_prefix() -> None:
    assert api_base_url(f"{SERVICE_URL}/") == f"{SERVICE_URL}{API_PREFIX}"


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
    with AtelierApi(SERVICE_URL, transport=httpx.MockTransport(_echoing)) as api:
        get_answer = api.get("/health", timeout=1.0)
        post_answer = api.post("/runs", b"{}", timeout=1.0)

    assert f"{API_PREFIX}/health".encode() in get_answer
    assert f"{API_PREFIX}/runs".encode() in post_answer
    assert b'"POST"' in post_answer


def test_a_closed_client_refuses_a_further_request() -> None:
    api = AtelierApi(SERVICE_URL, transport=httpx.MockTransport(_echoing))
    api.close()

    with pytest.raises(RuntimeError):
        api.get("/health", timeout=1.0)


def _refusing(status: int, body: bytes) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handle)


def test_a_non_2xx_answer_carries_its_status_and_exact_body() -> None:
    with (
        AtelierApi(SERVICE_URL, transport=_refusing(404, b'{"detail":"gone"}')) as api,
        pytest.raises(AtelierApiTransportFailure) as failure,
    ):
        api.get("/runs/missing", timeout=1.0)

    assert failure.value.status == 404
    assert failure.value.body == b'{"detail":"gone"}'
    assert failure.value.url.endswith("/runs/missing")


def _chunked(*chunks: bytes) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, content=iter(chunks))

    return httpx.MockTransport(handle)


def test_an_oversized_refusal_body_is_read_only_up_to_the_bound() -> None:
    chunks = tuple(b"x" * 1_000 for _ in range(10))
    with (
        AtelierApi(SERVICE_URL, transport=_chunked(*chunks)) as api,
        pytest.raises(AtelierApiTransportFailure) as failure,
    ):
        api.get("/runs/missing", timeout=1.0)

    assert len(failure.value.body) <= MAXIMUM_FAILURE_BODY_BYTES + len(chunks[0])
    assert len(failure.value.body) < sum(len(chunk) for chunk in chunks)


def test_an_unreachable_service_carries_no_status() -> None:
    def refuse_to_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with (
        AtelierApi(
            SERVICE_URL, transport=httpx.MockTransport(refuse_to_connect)
        ) as api,
        pytest.raises(AtelierApiTransportFailure) as failure,
    ):
        api.get("/health", timeout=1.0)

    assert failure.value.status is None
    assert failure.value.body == b""


def _streaming(*frames: str) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content="".join(frames).encode())

    return httpx.MockTransport(handle)


def test_event_lines_yields_every_line_of_a_successful_stream() -> None:
    with AtelierApi(
        SERVICE_URL, transport=_streaming("data: one\n\n", "data: two\n\n")
    ) as api:
        lines: Iterator[str] = api.event_lines(
            "/runs/r1/events", accept="text/event-stream"
        )

        assert list(lines) == ["data: one", "", "data: two", ""]


def test_event_lines_raises_the_transport_failure_before_yielding_a_refused_stream() -> (
    None
):
    with (
        AtelierApi(SERVICE_URL, transport=_refusing(404, b"not found")) as api,
        pytest.raises(AtelierApiTransportFailure) as failure,
    ):
        list(api.event_lines("/runs/missing/events", accept="text/event-stream"))

    assert failure.value.status == 404


def _raw_bytes(content: bytes) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=content)

    return httpx.MockTransport(handle)


def test_event_lines_refuses_bytes_that_are_not_utf8_text() -> None:
    with (
        AtelierApi(SERVICE_URL, transport=_raw_bytes(b"data: \xff\xfe\n\n")) as api,
        pytest.raises(AtelierApiTransportFailure) as failure,
    ):
        list(api.event_lines("/runs/r1/events", accept="text/event-stream"))

    assert failure.value.status is None


def test_event_lines_reports_a_connect_failure_with_no_status() -> None:
    def refuse_to_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with (
        AtelierApi(
            SERVICE_URL, transport=httpx.MockTransport(refuse_to_connect)
        ) as api,
        pytest.raises(AtelierApiTransportFailure) as failure,
    ):
        list(api.event_lines("/runs/r1/events", accept="text/event-stream"))

    assert failure.value.status is None
