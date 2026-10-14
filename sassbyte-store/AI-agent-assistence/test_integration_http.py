from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import integration_http


class IntegrationHttpTests(unittest.TestCase):
    def test_rejects_private_and_reserved_literal_destinations(self):
        for address in (
            "127.0.0.1",
            "10.0.0.8",
            "169.254.169.254",
            "::1",
            "fc00::1",
            "224.0.0.1",
        ):
            with self.subTest(address=address):
                with self.assertRaisesRegex(
                    integration_http.IntegrationEndpointError,
                    "private, local",
                ):
                    integration_http.resolve_public_https_target(
                        f"https://[{address}]/webhook"
                        if ":" in address
                        else f"https://{address}/webhook"
                    )

    def test_rejects_http_credentials_fragments_and_nonstandard_ports(self):
        invalid_urls = (
            "http://hooks.example/webhook",
            "https://user:pass@hooks.example/webhook",
            "https://hooks.example/webhook#fragment",
            "https://hooks.example:8443/webhook",
            "https://hooks.example:0/webhook",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(integration_http.IntegrationEndpointError):
                    integration_http.resolve_public_https_target(url)

    def test_rejects_hostnames_resolving_to_any_private_address(self):
        public_answer = (
            "hooks.example",
            443,
            0,
            "",
            ("203.0.113.10", 443),
        )
        private_answer = (
            "hooks.example",
            443,
            0,
            "",
            ("10.0.0.9", 443),
        )
        with patch.object(
            integration_http.socket,
            "getaddrinfo",
            return_value=[
                (2, 1, 6, "", public_answer[4]),
                (2, 1, 6, "", private_answer[4]),
            ],
        ):
            with self.assertRaisesRegex(
                integration_http.IntegrationEndpointError, "private, local"
            ):
                integration_http.resolve_public_https_target(
                    "https://hooks.example/webhook"
                )

    def test_pins_public_dns_resolution_and_sends_json_once(self):
        answer = ("hooks.example", 443, 0, "", ("8.8.8.8", 443))
        connection = Mock()
        connection.getresponse.return_value.status = 204
        with (
            patch.object(
                integration_http.socket,
                "getaddrinfo",
                return_value=[(2, 1, 6, "", answer[4])],
            ),
            patch.object(
                integration_http,
                "_PinnedHTTPSConnection",
                return_value=connection,
            ) as connection_factory,
        ):
            success, detail = integration_http.post_public_json(
                "https://hooks.example/api?event=lead",
                b'{"name":"Lead"}',
                {"Content-Type": "application/json"},
            )
        self.assertTrue(success)
        self.assertEqual(detail, "HTTP 204")
        connection_factory.assert_called_once_with("hooks.example", 443, "8.8.8.8")
        connection.request.assert_called_once_with(
            "POST",
            "/api?event=lead",
            body=b'{"name":"Lead"}',
            headers={"Content-Type": "application/json"},
        )
        connection.close.assert_called_once()

    def test_does_not_follow_redirect_responses(self):
        answer = ("hooks.example", 443, 0, "", ("8.8.8.8", 443))
        connection = Mock()
        connection.getresponse.return_value.status = 302
        with (
            patch.object(
                integration_http.socket,
                "getaddrinfo",
                return_value=[(2, 1, 6, "", answer[4])],
            ),
            patch.object(
                integration_http,
                "_PinnedHTTPSConnection",
                return_value=connection,
            ),
        ):
            success, detail = integration_http.post_public_json(
                "https://hooks.example/api",
                b"{}",
                {"Content-Type": "application/json"},
            )
        self.assertFalse(success)
        self.assertEqual(detail, "HTTP 302")

    def test_https_connection_pins_socket_and_keeps_original_tls_name(self):
        connection = integration_http._PinnedHTTPSConnection(
            "hooks.example", 443, "8.8.8.8"
        )
        raw_socket = Mock()
        secure_socket = Mock()
        context = Mock()
        context.wrap_socket.return_value = secure_socket
        with (
            patch.object(integration_http.socket, "create_connection", return_value=raw_socket) as connect,
            patch.object(integration_http.ssl, "create_default_context", return_value=context),
        ):
            connection = integration_http._PinnedHTTPSConnection(
                "hooks.example", 443, "8.8.8.8"
            )
            connection.connect()
        connect.assert_called_once_with(("8.8.8.8", 443), 12, None)
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="hooks.example")
        self.assertIs(connection.sock, secure_socket)


if __name__ == "__main__":
    unittest.main()
