from __future__ import annotations

import json
import http.client
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch
from uuid import uuid4

import app
import auth
import database


class HandlerAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()

        @contextmanager
        def connection():
            yield self.db

        self.database_init = patch.object(app, "initialize_runtime_database")
        self.database_init.start()
        self.connection = patch.object(database, "connection", connection)
        self.connection.start()
        self.session = patch.object(app.Handler, "current_session", return_value=None)
        self.current_session = self.session.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.current_session.stop()
        self.connection.stop()
        self.database_init.stop()

    def request(self, path, method="GET", payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def redirect_request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        headers = {}
        for name, value in response.getheaders():
            key = name.lower()
            if key == "set-cookie":
                headers.setdefault(key, []).append(value)
            else:
                headers[key] = value
        body = response.read()
        connection.close()
        return response.status, dict(headers), body

    def test_private_api_rejects_requests_without_a_session(self):
        status, body = self.request("/api/dashboard")
        self.assertEqual(status, 401)
        self.assertIn("Sign in", body["error"])

    def test_create_lead_rejects_unauthenticated_post(self):
        status, body = self.request(
            "/api/leads",
            method="POST",
            payload={"name": "Jordan Lee", "phone": "555-0100"},
        )
        self.assertEqual(status, 401)
        self.assertIn("Sign in", body["error"])

    def test_authenticated_write_requires_csrf_token(self):
        workspace_id = uuid4()
        self.current_session.return_value = {"workspace_id": workspace_id}
        status, body = self.request(
            "/api/settings",
            method="POST",
            payload={"auto_followup_enabled": True},
        )
        self.assertEqual(status, 403)
        self.assertIn("CSRF", body["error"])

    def test_authenticated_dashboard_uses_session_workspace(self):
        workspace_id = uuid4()
        self.current_session.return_value = {"workspace_id": workspace_id}
        with patch.object(database, "dashboard", return_value={"lead_count": 0}) as dashboard:
            status, body = self.request("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(body["lead_count"], 0)
        dashboard.assert_called_once_with(self.db, workspace_id)

    def test_login_sets_http_only_session_and_csrf_cookies(self):
        credentials = auth.SessionCredentials(
            "session-secret", "csrf-secret", datetime.now(timezone.utc)
        )
        with (
            patch.object(auth, "authenticate_password", return_value={
                "id": 7, "email": "user@example.com", "display_name": "User",
            }),
            patch.object(auth, "issue_session", return_value=credentials),
        ):
            request = Request(
                f"{self.base_url}/api/auth/login",
                data=json.dumps({
                    "email": "user@example.com",
                    "password": "long-safe-password-value",
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
                cookies = response.headers.get_all("Set-Cookie")
                self.assertTrue(any("openlead_session=session-secret" in cookie for cookie in cookies))
                self.assertTrue(any("openlead_csrf=csrf-secret" in cookie for cookie in cookies))
                self.assertTrue(any("HttpOnly" in cookie for cookie in cookies))

    def test_logout_revokes_session_and_clears_cookies(self):
        csrf_token = "csrf-secret"
        self.current_session.return_value = {
            "csrf_digest": auth.digest_token(csrf_token),
            "workspace_id": uuid4(),
        }
        with patch.object(auth, "revoke_session") as revoke_session:
            request = Request(
                f"{self.base_url}/api/auth/logout",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Origin": self.base_url,
                    "Cookie": f"{auth.SESSION_COOKIE}=session-secret; openlead_csrf={csrf_token}",
                    auth.CSRF_HEADER: csrf_token,
                },
                method="POST",
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(any(
                    "Max-Age=0" in cookie
                    for cookie in response.headers.get_all("Set-Cookie")
                ))
        revoke_session.assert_called_once_with(self.db, "session-secret")

    def test_unavailable_database_fails_closed(self):
        with patch.object(
            app,
            "initialize_runtime_database",
            side_effect=database.DatabaseNotConfigured("not configured"),
        ):
            status, body = self.request("/api/dashboard")
        self.assertEqual(status, 503)
        self.assertIn("temporarily unavailable", body["error"])

    def test_google_signin_starts_pkce_flow_and_sets_temporary_cookies(self):
        config = {
            "OPENLEAD_GOOGLE_CLIENT_ID": "client-id",
            "OPENLEAD_GOOGLE_CLIENT_SECRET": "client-secret",
            "OPENLEAD_GOOGLE_REDIRECT_URI": "https://openlead.example/api/auth/google/callback",
            "public_url": "https://openlead.example",
        }
        with (
            patch.object(app.Handler, "google_oauth_configuration", return_value=config),
            patch.object(
                database, "create_google_oauth_state",
                return_value=("state-value", "nonce-value", "verifier-value"),
            ),
        ):
            status, headers, _ = self.redirect_request("/api/auth/google")
        self.assertEqual(status, 303)
        location = urlsplit(headers["location"])
        query = parse_qs(location.query)
        self.assertEqual(location.netloc, "accounts.google.com")
        self.assertEqual(query["state"], ["state-value"])
        self.assertEqual(query["nonce"], ["nonce-value"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        cookies = headers["set-cookie"]
        self.assertEqual(len(cookies), 3)
        self.assertTrue(all("HttpOnly" in cookie and "SameSite=Lax" in cookie for cookie in cookies))

    def test_google_callback_rejects_state_mismatch(self):
        with (
            patch.object(
                app.Handler, "google_oauth_configuration",
                return_value={"public_url": "https://openlead.example"},
            ),
            patch.object(app.Handler, "exchange_google_code") as exchange,
        ):
            status, headers, _ = self.redirect_request(
                "/api/auth/google/callback?" + urlencode({"code": "code", "state": "wrong"}),
                headers={
                    "Cookie": (
                        "openlead_oauth_state=expected; "
                        "openlead_oauth_nonce=nonce; "
                        "openlead_oauth_verifier=verifier"
                    )
                },
            )
        self.assertEqual(status, 303)
        self.assertIn("auth_error=google", headers["location"])
        exchange.assert_not_called()

    def test_google_callback_validates_nonce_and_issues_session(self):
        config = {
            "OPENLEAD_GOOGLE_CLIENT_ID": "client-id",
            "OPENLEAD_GOOGLE_CLIENT_SECRET": "client-secret",
            "OPENLEAD_GOOGLE_REDIRECT_URI": "https://openlead.example/api/auth/google/callback",
            "public_url": "https://openlead.example",
        }
        credentials = auth.SessionCredentials(
            "session-secret", "csrf-secret", datetime.now(timezone.utc)
        )
        with (
            patch.object(app.Handler, "google_oauth_configuration", return_value=config),
            patch.object(database, "consume_google_oauth_state", return_value=True),
            patch.object(app.Handler, "exchange_google_code", return_value={
                "nonce": "nonce-value",
                "email_verified": True,
                "email": "google@example.com",
                "sub": "google-subject",
                "name": "Google User",
            }),
            patch.object(auth, "authenticate_google_identity", return_value={
                "id": 7, "email": "google@example.com", "display_name": "Google User",
            }),
            patch.object(auth, "issue_session", return_value=credentials),
        ):
            status, headers, _ = self.redirect_request(
                "/api/auth/google/callback?" + urlencode({
                    "code": "authorization-code", "state": "state-value",
                }),
                headers={
                    "Cookie": (
                        "openlead_oauth_state=state-value; "
                        "openlead_oauth_nonce=nonce-value; "
                        "openlead_oauth_verifier=pkce-verifier"
                    )
                },
            )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "https://openlead.example/")
        cookies = headers["set-cookie"]
        self.assertTrue(any("openlead_session=session-secret" in cookie for cookie in cookies))
        self.assertTrue(any("openlead_csrf=csrf-secret" in cookie for cookie in cookies))
        self.assertTrue(any("openlead_oauth_state=" in cookie and "Max-Age=0" in cookie for cookie in cookies))


class AppBehaviorTests(unittest.TestCase):
    def test_http_redirect_handler_refuses_redirects(self):
        redirect = app._NoRedirectHandler()
        self.assertIsNone(
            redirect.redirect_request(
                Mock(), Mock(), 302, "Found", {}, "https://attacker.example/"
            )
        )

    def test_qualification_uses_rules_when_public_llm_is_unavailable(self):
        lead = {
            "name": "Jordan",
            "phone": "555",
            "email": "j@example.com",
            "timeline": "30 days",
            "budget": "$200k",
            "notes": "Looking to sell a property soon.",
        }
        with patch.object(app, "public_llm_chat", side_effect=OSError("offline")):
            result = app.qualify_lead(lead, dict(app.DEFAULTS))
        self.assertEqual(result["provider"], "rules")
        self.assertEqual(result["score"], 90)

    def test_public_settings_never_exposes_any_integration_secret(self):
        settings = dict(
            app.DEFAULTS,
            crm_token="crm-secret",
            webhook_key="webhook-secret",
            ai_api_key="llm-secret",
        )
        public = app.public_settings(settings)
        for secret in ("crm_token", "webhook_key", "ai_api_key"):
            self.assertNotIn(secret, public)
        self.assertTrue(public["crm_token_configured"])
        self.assertTrue(public["webhook_key_configured"])
        self.assertTrue(public["ai_api_key_configured"])

    def test_public_llm_openai_uses_chat_completions_endpoint(self):
        result = {"choices": [{"message": {"content": "OpenAI reply"}}]}
        settings = {
            "ai_provider": "openai",
            "ai_model": "gpt-test",
            "ai_api_key": "api-secret",
        }
        with patch.object(app, "_post_llm_json", return_value=result) as request:
            answer = app.public_llm_chat(
                settings,
                [{"role": "system", "content": "Instructions"}, {"role": "user", "content": "Hello"}],
                json_mode=True,
            )
        self.assertEqual(answer, "OpenAI reply")
        request.assert_called_once()
        endpoint, payload, headers = request.call_args.args
        self.assertEqual(endpoint, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(headers["Authorization"], "Bearer api-secret")

    def test_public_llm_anthropic_uses_messages_api(self):
        result = {"content": [{"type": "text", "text": "Claude reply"}]}
        settings = {
            "ai_provider": "anthropic",
            "ai_model": "claude-test",
            "ai_api_key": "api-secret",
        }
        with patch.object(app, "_post_llm_json", return_value=result) as request:
            answer = app.public_llm_chat(
                settings,
                [{"role": "system", "content": "Instructions"}, {"role": "user", "content": "Hello"}],
            )
        self.assertEqual(answer, "Claude reply")
        endpoint, payload, headers = request.call_args.args
        self.assertEqual(endpoint, "https://api.anthropic.com/v1/messages")
        self.assertEqual(payload["system"], "Instructions")
        self.assertEqual(headers["x-api-key"], "api-secret")

    def test_public_llm_gemini_uses_generate_content_api(self):
        result = {
            "candidates": [{
                "content": {"parts": [{"text": '{"answer":"Gemini reply"}'}]},
            }],
        }
        settings = {
            "ai_provider": "gemini",
            "ai_model": "gemini-test",
            "ai_api_key": "api-secret",
        }
        with patch.object(app, "_post_llm_json", return_value=result) as request:
            answer = app.public_llm_chat(
                settings,
                [{"role": "system", "content": "Instructions"}, {"role": "user", "content": "Hello"}],
                json_mode=True,
            )
        self.assertEqual(answer, '{"answer":"Gemini reply"}')
        endpoint, payload, headers = request.call_args.args
        self.assertEqual(
            endpoint,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent",
        )
        self.assertEqual(payload["generationConfig"], {"responseMimeType": "application/json"})
        self.assertEqual(headers["x-goog-api-key"], "api-secret")

    def test_public_llm_requires_provider_model_and_key(self):
        with self.assertRaisesRegex(ValueError, "Select OpenAI"):
            app.public_llm_chat({}, [{"role": "user", "content": "Hello"}])
        with self.assertRaisesRegex(ValueError, "model and API key"):
            app.public_llm_chat(
                {"ai_provider": "openai", "ai_model": "", "ai_api_key": ""},
                [{"role": "user", "content": "Hello"}],
            )

    def test_public_settings_never_exposes_secrets(self):
        settings = dict(
            app.DEFAULTS,
            crm_token="secret",
            webhook_key="another-secret",
            ai_api_key="provider-secret",
        )
        public = app.public_settings(settings)
        self.assertNotIn("crm_token", public)
        self.assertNotIn("webhook_key", public)
        self.assertNotIn("ai_api_key", public)
        self.assertTrue(public["crm_token_configured"])
        self.assertTrue(public["webhook_key_configured"])
        self.assertTrue(public["ai_api_key_configured"])

    def test_stage_allowlist_excludes_arbitrary_values(self):
        self.assertIn("qualified", app.STAGES)
        self.assertNotIn("drop_database", app.STAGES)

    def test_google_callback_query_is_redacted_from_application_logs(self):
        handler = object.__new__(app.Handler)
        handler.path = "/api/auth/google/callback?code=secret-code&state=secret-state"
        with (
            patch.object(handler, "log_date_time_string", return_value="now"),
            patch("builtins.print") as output,
        ):
            handler.log_message(
                '"%s" %s',
                "GET /api/auth/google/callback?code=secret-code&state=secret-state HTTP/1.1",
                "303",
            )
        self.assertNotIn("secret-code", output.call_args.args[0])
        self.assertNotIn("secret-state", output.call_args.args[0])
        self.assertIn("[redacted]", output.call_args.args[0])

    def test_google_exchange_verifies_signed_token_for_configured_client(self):
        class TokenResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return json.dumps({"id_token": "signed-id-token"}).encode()

        config = {
            "OPENLEAD_GOOGLE_CLIENT_ID": "client-id",
            "OPENLEAD_GOOGLE_CLIENT_SECRET": "private-client-secret",
            "OPENLEAD_GOOGLE_REDIRECT_URI": "https://openlead.example/api/auth/google/callback",
        }
        claims = {"sub": "google-subject", "email_verified": True}
        with (
            patch.object(app, "_urlopen_no_redirect", return_value=TokenResponse()) as urlopen_mock,
            patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims) as verify,
        ):
            result = app.Handler.exchange_google_code(
                object.__new__(app.Handler), "auth-code", "pkce-verifier", config
            )
        self.assertEqual(result, claims)
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[0], "signed-id-token")
        self.assertEqual(verify.call_args.args[2], "client-id")
        posted = parse_qs(urlopen_mock.call_args.args[0].data.decode())
        self.assertEqual(posted["code_verifier"], ["pkce-verifier"])
        self.assertEqual(posted["client_secret"], ["private-client-secret"])

    def test_google_exchange_rejects_unverified_email_claim(self):
        class TokenResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return json.dumps({"id_token": "signed-id-token"}).encode()

        config = {
            "OPENLEAD_GOOGLE_CLIENT_ID": "client-id",
            "OPENLEAD_GOOGLE_CLIENT_SECRET": "client-secret",
            "OPENLEAD_GOOGLE_REDIRECT_URI": "https://openlead.example/api/auth/google/callback",
        }
        with (
            patch.object(app, "_urlopen_no_redirect", return_value=TokenResponse()),
            patch("google.oauth2.id_token.verify_oauth2_token", return_value={
                "sub": "google-subject", "email_verified": False,
            }),
            self.assertRaisesRegex(ValueError, "email is not verified"),
        ):
            app.Handler.exchange_google_code(
                object.__new__(app.Handler), "auth-code", "pkce-verifier", config
            )


if __name__ == "__main__":
    unittest.main()
