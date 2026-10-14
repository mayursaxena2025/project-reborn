from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit


MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 12


class IntegrationEndpointError(ValueError):
    pass


def resolve_public_https_target(url: str) -> tuple[Any, str, int, str]:
    try:
        parsed = urlsplit(url.strip())
        hostname = parsed.hostname
        port = parsed.port if parsed.port is not None else 443
    except (ValueError, AttributeError) as error:
        raise IntegrationEndpointError("Use a valid public HTTPS webhook URL.") from error
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port != 443
        or any(ord(character) < 32 for character in url)
    ):
        raise IntegrationEndpointError(
            "Webhook endpoints must be public HTTPS URLs on port 443 without credentials or fragments."
        )
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as error:
        raise IntegrationEndpointError("Webhook hostname is invalid.") from error
    if not ascii_hostname:
        raise IntegrationEndpointError("Webhook hostname is invalid.")

    try:
        literal_ip = ipaddress.ip_address(ascii_hostname)
    except ValueError:
        try:
            answers = socket.getaddrinfo(
                ascii_hostname, port, type=socket.SOCK_STREAM
            )
        except OSError as error:
            raise IntegrationEndpointError("Webhook hostname could not be resolved.") from error
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    else:
        addresses = [str(literal_ip)]
    if not addresses:
        raise IntegrationEndpointError("Webhook hostname has no address.")
    try:
        ip_addresses = [ipaddress.ip_address(address.split("%", 1)[0]) for address in addresses]
    except ValueError as error:
        raise IntegrationEndpointError("Webhook hostname resolved to an invalid address.") from error
    if any(not address.is_global or address.is_multicast for address in ip_addresses):
        raise IntegrationEndpointError(
            "Webhook endpoints cannot resolve to private, local, link-local, or reserved networks."
        )
    return parsed, ascii_hostname, port, addresses[0]


def _request_target(parsed: Any) -> str:
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    return path


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str):
        super().__init__(host, port, timeout=REQUEST_TIMEOUT_SECONDS, context=ssl.create_default_context())
        self._resolved_address = address

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._resolved_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def post_public_json(
    url: str, payload: bytes, headers: dict[str, str]
) -> tuple[bool, str]:
    parsed, hostname, port, address = resolve_public_https_target(url)
    path = _request_target(parsed)
    connection = _PinnedHTTPSConnection(hostname, port, address)
    try:
        connection.request("POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        response.read(MAX_RESPONSE_BYTES + 1)
        if 200 <= response.status < 300:
            return True, f"HTTP {response.status}"
        return False, f"HTTP {response.status}"
    finally:
        connection.close()
