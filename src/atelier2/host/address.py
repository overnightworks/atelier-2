"""Where the local service listens, and where a client reaches it.

Serving and running are the two sides of one default: change where the server
binds and the run command must follow, or an operator who ran `serve` with no
arguments cannot run anything on it. They live here, and not with the server,
because reading this pair must not cost the client the whole server graph.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8422
ADDRESSABLE_SCHEMES = frozenset({"http", "https"})
LOOPBACK_SERVICE_SCHEME = "http"


def loopback_service_url(port: int, path: str = "") -> str:
    """Where a client on this machine reaches a service bound to loopback.

    The one place such an address is spelled: this machine is the whole trust
    boundary of a loopback service, so it carries no certificate and no caller
    picks a scheme of its own.
    """

    return urlunsplit((LOOPBACK_SERVICE_SCHEME, f"{DEFAULT_HOST}:{port}", path, "", ""))


DEFAULT_SERVICE_URL = loopback_service_url(DEFAULT_PORT)


def is_loopback_host(host: str) -> bool:
    """Whether this host string is a literal loopback IP address.

    A name resolves elsewhere at connect or bind time, so a host this function
    cannot read as an address is not trusted as loopback. This is the single
    owner of that host-level trust rule; callers add why a non-loopback host is
    refused for their boundary.
    """

    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def is_loopback_service_url(service_url: str) -> bool:
    """Whether this client address is a literal loopback host.

    A name resolves elsewhere at connect time, so a host this function cannot
    read as an address is not loopback. The MCP child has no caller
    authentication; only a loopback service keeps that the same trust as the
    browser on this machine.
    """

    address = urlsplit(service_url)
    host = address.hostname
    if address.scheme not in ADDRESSABLE_SCHEMES or not host:
        return False
    return is_loopback_host(host)
