from __future__ import annotations

import json
import os
import re
import hashlib
import hmac
import base64
import http.client
import threading
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import auth
import database
import integration_http
import psycopg
import requests
from google.auth.exceptions import GoogleAuthError


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DEFAULTS = database.DEFAULTS | {key: "" for key in database.SECRET_KEYS}
STAGES = database.STAGES
_runtime_database_initialized = False
_runtime_database_lock = threading.Lock()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _urlopen_no_redirect(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(_NoRedirectHandler)
    return opener.open(request, timeout=timeout)


def initialize_runtime_database() -> None:
    global _runtime_database_initialized
    if _runtime_database_initialized:
        return
    with _runtime_database_lock:
        if not _runtime_database_initialized:
            database.initialize_database()
            _runtime_database_initialized = True


def public_settings(settings: dict[str, str]) -> dict[str, Any]:
    return {
        "crm_leads_url": settings["crm_leads_url"],
        "sms_webhook_url": settings["sms_webhook_url"],
        "voice_webhook_url": settings["voice_webhook_url"],
        "calendar_webhook_url": settings["calendar_webhook_url"],
        "ai_provider": settings["ai_provider"],
        "ai_model": settings["ai_model"],
        "ai_api_key_configured": bool(settings["ai_api_key"]),
        "auto_followup_enabled": settings["auto_followup_enabled"] == "true",
        "crm_token_configured": bool(settings["crm_token"]),
        "webhook_key_configured": bool(settings["webhook_key"]),
    }


def local_qualification(lead: dict[str, Any]) -> dict[str, Any]:
    score = 30
    if lead.get("phone"):
        score += 10
    if lead.get("email"):
        score += 10
    if lead.get("timeline"):
        score += 15
    if lead.get("budget"):
        score += 15
    if len(lead.get("notes", "")) >= 30:
        score += 10
    score = min(score, 100)
    signal = "High intent" if score >= 65 else "Needs more information"
    next_step = "Offer a consultation" if score >= 65 else "Ask about timeline and goals"
    return {
        "score": score,
        "summary": signal,
        "rationale": "Local rules-based estimate; connect a public LLM for model-assisted qualification.",
        "next_step": next_step,
        "provider": "rules",
    }


def _post_llm_json(
    endpoint: str, payload: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with _urlopen_no_redirect(request, timeout=30) as response:
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"LLM provider returned HTTP {response.status}.")
            body = response.read(1_000_001)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError(f"LLM provider returned HTTP {error.code}.") from error
    if len(body) > 1_000_000:
        raise ValueError("LLM provider response is too large.")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("LLM provider returned an invalid response.")
    return result


def public_llm_chat(
    settings: dict[str, str], messages: list[dict[str, str]], json_mode: bool = False
) -> str:
    provider = settings.get("ai_provider", "")
    model = settings.get("ai_model", "").strip()
    api_key = settings.get("ai_api_key", "").strip()
    if provider not in {"openai", "anthropic", "gemini"}:
        raise ValueError("Select OpenAI, Anthropic, or Gemini in Connections first.")
    if not model or not api_key:
        raise ValueError("Configure the AI model and API key in Connections first.")

    system_messages = [message["content"] for message in messages if message["role"] == "system"]
    conversation = [
        {"role": message["role"], "content": message["content"]}
        for message in messages
        if message["role"] != "system"
    ]
    if provider == "openai":
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        result = _post_llm_json(
            "https://api.openai.com/v1/chat/completions",
            payload,
            {"Authorization": f"Bearer {api_key}"},
        )
        return str(result["choices"][0]["message"]["content"])

    if provider == "anthropic":
        payload = {
            "model": model,
            "max_tokens": 1024,
            "system": "\n\n".join(system_messages),
            "messages": conversation,
        }
        if json_mode:
            payload["system"] += "\nReturn only a valid JSON object."
        result = _post_llm_json(
            "https://api.anthropic.com/v1/messages",
            payload,
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        return "".join(
            str(block.get("text", ""))
            for block in result["content"]
            if block.get("type") == "text"
        )

    contents = [
        {
            "role": "model" if message["role"] == "assistant" else "user",
            "parts": [{"text": message["content"]}],
        }
        for message in conversation
    ]
    payload = {"contents": contents}
    if system_messages:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_messages)}]}
    if json_mode:
        payload["generationConfig"] = {"responseMimeType": "application/json"}
    result = _post_llm_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='')}:generateContent",
        payload,
        {"x-goog-api-key": api_key},
    )
    return "".join(
        str(part.get("text", ""))
        for part in result["candidates"][0]["content"]["parts"]
        if "text" in part
    )


def qualify_lead(lead: dict[str, Any], settings: dict[str, str]) -> dict[str, Any]:
    context = json.dumps({key: lead.get(key, "") for key in ("name", "source", "timeline", "budget", "notes")})
    try:
        content = public_llm_chat(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "Qualify a real-estate sales lead. Return JSON only with integer score 0-100, "
                        "brief summary, rationale, and one next_step. Do not infer facts not supplied."
                    ),
                },
                {"role": "user", "content": context},
            ],
            json_mode=True,
        )
        result = json.loads(content)
        result["score"] = max(0, min(100, int(result["score"])))
        result["provider"] = settings["ai_provider"]
        return result
    except (
        OSError,
        TimeoutError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        return local_qualification(lead)


def post_json(url: str, payload: dict[str, Any], token: str = "") -> tuple[bool, str]:
    if not url:
        return False, "No endpoint configured."
    try:
        return integration_http.post_public_json(
            url,
            json.dumps(payload).encode(),
            {
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
    except integration_http.IntegrationEndpointError as error:
        return False, str(error)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        return False, f"Webhook request failed ({type(error).__name__})."


def lead_payload(lead: dict[str, Any]) -> dict[str, Any]:
    return {key: lead[key] for key in (
        "id", "name", "phone", "email", "source", "timeline", "budget", "notes",
        "consent_sms", "consent_call", "stage", "score", "qualification", "created_at",
    )}


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenLeadAgent/0.3"
    session_cookie_path = "/openlead/"

    def log_message(self, format_string: str, *args: Any) -> None:
        safe_args = list(args)
        if self.path.startswith("/api/auth/google/callback?"):
            for index, argument in enumerate(safe_args):
                request_parts = str(argument).split(" ", 2)
                if (
                    len(request_parts) == 3
                    and request_parts[1].startswith("/api/auth/google/callback?")
                ):
                    safe_args[index] = (
                        f"{request_parts[0]} "
                        f"{request_parts[1].split('?', 1)[0]}?[redacted] "
                        f"{request_parts[2]}"
                    )
                    break
        print(f"[{self.log_date_time_string()}] {format_string % tuple(safe_args)}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'self'; frame-ancestors 'none'")
        for cookie in getattr(self, "_response_cookies", []):
            self.send_header("Set-Cookie", cookie)
        self._response_cookies = []
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location: str, status: int = 303) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for cookie in getattr(self, "_response_cookies", []):
            self.send_header("Set-Cookie", cookie)
        self._response_cookies = []
        self.end_headers()

    def auth_cookie(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(auth.SESSION_COOKIE)
        return morsel.value if morsel else None

    def csrf_cookie(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("openlead_csrf")
        return morsel.value if morsel else None

    def set_auth_cookies(self, credentials: auth.SessionCredentials) -> None:
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        suffix = "; Path=/; SameSite=Strict" + ("; Secure" if secure else "")
        self._response_cookies = [
            f"{auth.SESSION_COOKIE}={credentials.token}; Max-Age={int(auth.SESSION_LIFETIME.total_seconds())}; HttpOnly{suffix}",
            f"openlead_csrf={credentials.csrf_token}; Max-Age={int(auth.SESSION_LIFETIME.total_seconds())}{suffix}",
        ]

    def clear_auth_cookies(self) -> None:
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        suffix = "; Path=/; Max-Age=0; SameSite=Strict" + ("; Secure" if secure else "")
        self._response_cookies = [
            f"{auth.SESSION_COOKIE}={suffix}; HttpOnly",
            f"openlead_csrf={suffix}",
        ]

    def set_google_oauth_cookies(self, state: str, nonce: str, verifier: str) -> None:
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        suffix = "; Path=/; Max-Age=600; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
        self._response_cookies = [
            f"openlead_oauth_state={state}{suffix}",
            f"openlead_oauth_nonce={nonce}{suffix}",
            f"openlead_oauth_verifier={verifier}{suffix}",
        ]

    def oauth_cookie(self, name: str) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(name)
        return morsel.value if morsel else None

    def clear_google_oauth_cookies(self) -> list[str]:
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        suffix = "; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
        return [
            f"openlead_oauth_state={suffix}",
            f"openlead_oauth_nonce={suffix}",
            f"openlead_oauth_verifier={suffix}",
        ]

    def origin_is_same_site(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed_origin = urlsplit(origin)
        except ValueError:
            return False
        forwarded_host = self.headers.get("X-Forwarded-Host", self.headers.get("Host", ""))
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "http")
        return parsed_origin.netloc.lower() == forwarded_host.lower() and parsed_origin.scheme.lower() == forwarded_proto.lower()

    def email_configuration(self) -> dict[str, Any] | None:
        names = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "OPENLEAD_PUBLIC_URL")
        values = {name: os.environ.get(name, "").strip() for name in names}
        if not all(values.values()) or not os.environ.get("OPENLEAD_DATABASE_URL"):
            return None
        try:
            auth.validate_email(values["SMTP_FROM"])
            database._fernet()
            public_url = urlsplit(values["OPENLEAD_PUBLIC_URL"])
            smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        except (ValueError, database.IntegrationSecretsNotConfigured):
            return None
        if (
            public_url.scheme != "https"
            or not public_url.netloc
            or public_url.query
            or public_url.fragment
            or not 1 <= smtp_port <= 65535
        ):
            return None
        values["public_url"] = values["OPENLEAD_PUBLIC_URL"].rstrip("/")
        values["smtp_port"] = smtp_port
        return values

    def google_oauth_configuration(self) -> dict[str, str] | None:
        names = (
            "OPENLEAD_GOOGLE_CLIENT_ID",
            "OPENLEAD_GOOGLE_CLIENT_SECRET",
            "OPENLEAD_GOOGLE_REDIRECT_URI",
            "OPENLEAD_PUBLIC_URL",
        )
        values = {name: os.environ.get(name, "").strip() for name in names}
        if not all(values.values()):
            return None
        try:
            public_url = urlsplit(values["OPENLEAD_PUBLIC_URL"])
            redirect_uri = urlsplit(values["OPENLEAD_GOOGLE_REDIRECT_URI"])
        except ValueError:
            return None
        if (
            public_url.scheme != "https"
            or not public_url.netloc
            or public_url.username
            or public_url.password
            or public_url.query
            or public_url.fragment
            or redirect_uri.scheme != "https"
            or not redirect_uri.netloc
            or redirect_uri.username
            or redirect_uri.password
            or redirect_uri.netloc.lower() != public_url.netloc.lower()
            or not redirect_uri.path.endswith("/api/auth/google/callback")
            or redirect_uri.query
            or redirect_uri.fragment
        ):
            return None
        values["public_url"] = values["OPENLEAD_PUBLIC_URL"].rstrip("/")
        return values

    def google_authorization_url(self, state: str, nonce: str, verifier: str, config: dict[str, str]) -> str:
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": config["OPENLEAD_GOOGLE_CLIENT_ID"],
            "redirect_uri": config["OPENLEAD_GOOGLE_REDIRECT_URI"],
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        })

    def exchange_google_code(
        self, code: str, verifier: str, config: dict[str, str]
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=urlencode({
                "code": code,
                "client_id": config["OPENLEAD_GOOGLE_CLIENT_ID"],
                "client_secret": config["OPENLEAD_GOOGLE_CLIENT_SECRET"],
                "redirect_uri": config["OPENLEAD_GOOGLE_REDIRECT_URI"],
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            }).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with _urlopen_no_redirect(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError("Google token exchange was rejected.")
            raw_response = response.read(65_537)
        if len(raw_response) > 65_536:
            raise ValueError("Google token response is too large.")
        token_response = json.loads(raw_response)
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or len(id_token) > 16_384:
            raise ValueError("Google did not return a valid identity token.")

        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token as google_id_token

        claims = google_id_token.verify_oauth2_token(
            id_token, GoogleRequest(), config["OPENLEAD_GOOGLE_CLIENT_ID"]
        )
        if not isinstance(claims, dict) or claims.get("email_verified") is not True:
            raise ValueError("Google account email is not verified.")
        return claims

    def handle_google_callback(self, query: str) -> None:
        config = self.google_oauth_configuration()
        if config is None:
            return self.send_json({"error": "Google sign-in is not configured."}, 503)
        params = parse_qs(query)
        code = params.get("code", [""])[0]
        returned_state = params.get("state", [""])[0]
        state = self.oauth_cookie("openlead_oauth_state")
        nonce = self.oauth_cookie("openlead_oauth_nonce")
        verifier = self.oauth_cookie("openlead_oauth_verifier")
        oauth_cookies = self.clear_google_oauth_cookies()
        fail_url = f"{config['public_url']}/?auth_error=google"
        if (
            params.get("error")
            or not code
            or not state
            or not nonce
            or not verifier
            or not hmac.compare_digest(returned_state, state)
        ):
            self._response_cookies = oauth_cookies
            return self.send_redirect(fail_url)
        try:
            with database.connection() as db:
                state_valid = database.consume_google_oauth_state(db, state, nonce, verifier)
            if not state_valid:
                self._response_cookies = oauth_cookies
                return self.send_redirect(fail_url)
            claims = self.exchange_google_code(code, verifier, config)
            token_nonce = claims.get("nonce")
            if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
                raise ValueError("Google identity nonce did not match.")
            email = auth.validate_email(str(claims.get("email", "")))
            subject = str(claims.get("sub", ""))
            display_name = str(claims.get("name") or email.partition("@")[0])
            with database.connection() as db:
                user = auth.authenticate_google_identity(db, email, display_name, subject)
                credentials = auth.issue_session(db, user["id"])
            self.set_auth_cookies(credentials)
            self._response_cookies.extend(oauth_cookies)
            return self.send_redirect(f"{config['public_url']}/")
        except (
            ValueError,
            KeyError,
            OSError,
            urllib.error.URLError,
            requests.RequestException,
            GoogleAuthError,
            psycopg.Error,
        ) as error:
            print(f"OpenLead Google sign-in failed: {type(error).__name__}")
            self._response_cookies = oauth_cookies
            return self.send_redirect(fail_url)

    def send_auth_email(self, recipient: str, subject: str, body: str) -> None:
        config = self.email_configuration()
        if config is None:
            raise RuntimeError("Account email delivery is not configured.")
        auth.deliver_email(
            recipient,
            subject,
            body,
            host=config["SMTP_HOST"],
            port=config["smtp_port"],
            username=config["SMTP_USER"],
            password=config["SMTP_PASSWORD"],
            sender=config["SMTP_FROM"],
        )

    def current_session(self, db: Any) -> dict[str, Any] | None:
        return auth.resolve_session(db, self.auth_cookie())

    def require_csrf(self, session: dict[str, Any]) -> bool:
        return (
            self.origin_is_same_site()
            and self.csrf_cookie() == self.headers.get(auth.CSRF_HEADER)
            and auth.validate_csrf(session, self.headers.get(auth.CSRF_HEADER))
        )

    def read_json(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 1_000_000:
            raise ValueError("Request body is too large.")
        payload = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object.")
        return payload

    def do_GET(self) -> None:
        route = urlsplit(self.path)
        if route.path == "/" or route.path == "/index.html":
            return self.send_file("index.html", "text/html; charset=utf-8")
        if route.path in ("/app.css", "/app.js"):
            return self.send_file(route.path.lstrip("/"), "text/css; charset=utf-8" if route.path.endswith("css") else "text/javascript; charset=utf-8")
        if not route.path.startswith("/api/"):
            return self.send_json({"error": "Not found"}, 404)
        try:
            initialize_runtime_database()
            if route.path == "/api/auth/google":
                config = self.google_oauth_configuration()
                if config is None:
                    return self.send_json({"error": "Google sign-in is not configured."}, 503)
                with database.connection() as db:
                    state, nonce, verifier = database.create_google_oauth_state(db)
                self.set_google_oauth_cookies(state, nonce, verifier)
                return self.send_redirect(self.google_authorization_url(state, nonce, verifier, config))
            if route.path == "/api/auth/google/callback":
                return self.handle_google_callback(route.query)
            if route.path == "/api/auth/config":
                ready = self.email_configuration() is not None
                google_ready = self.google_oauth_configuration() is not None
                return self.send_json({
                    "password_signup_available": ready,
                    "google_signin_available": google_ready,
                })
            with database.connection() as db:
                session = self.current_session(db)
                if route.path == "/api/auth/me":
                    if not session:
                        return self.send_json({"user": None}, 200)
                    csrf_token = self.csrf_cookie()
                    if not csrf_token or not auth.validate_csrf(session, csrf_token):
                        return self.send_json({"user": None}, 200)
                    return self.send_json({
                        "user": {
                            "id": session["user_id"],
                            "email": session["email"],
                            "display_name": session["display_name"],
                        },
                        "csrf_token": csrf_token,
                    })
                if not session:
                    return self.send_json({"error": "Sign in to access this workspace."}, 401)
                workspace_id = session["workspace_id"]
                if route.path == "/api/dashboard":
                    return self.send_json(database.dashboard(db, workspace_id))
                if route.path == "/api/leads":
                    query = parse_qs(route.query).get("q", [""])[0].strip()
                    return self.send_json(database.list_leads(db, workspace_id, query))
                if route.path == "/api/activities":
                    return self.send_json(database.list_activities(db, workspace_id))
                if route.path == "/api/settings":
                    return self.send_json(public_settings(database.get_settings(db, workspace_id)))
                match = re.fullmatch(r"/api/leads/(\d+)", route.path)
                if match:
                    lead = database.get_lead(db, workspace_id, int(match.group(1)))
                    return self.send_json(lead if lead else {"error": "Lead not found"}, 200 if lead else 404)
        except (RuntimeError, ValueError, psycopg.Error, OSError) as error:
            print(f"OpenLead database unavailable: {error}")
            return self.send_json({"error": "OpenLead account storage is temporarily unavailable."}, 503)
        return self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        route = urlsplit(self.path)
        if not route.path.startswith("/api/"):
            return self.send_json({"error": "Not found"}, 404)
        if not self.origin_is_same_site():
            return self.send_json({"error": "Cross-site request rejected."}, 403)
        try:
            payload = self.read_json()
            initialize_runtime_database()
            with database.connection() as db:
                if route.path == "/api/auth/register":
                    email_config = self.email_configuration()
                    if email_config is None:
                        return self.send_json({"error": "Account registration is not configured yet."}, 503)
                    try:
                        user_id, token = auth.create_account(
                            db,
                            str(payload.get("email", "")),
                            str(payload.get("display_name", "")),
                            str(payload.get("password", "")),
                        )
                    except auth.AuthenticationError as error:
                        return self.send_json({"error": str(error)}, 409)
                    verify_url = f"{email_config['public_url']}/?verify={quote(token)}"
                    self.send_auth_email(
                        str(payload["email"]).strip(),
                        "Verify your OpenLead account",
                        f"Verify your email to activate your OpenLead account:\n\n{verify_url}\n\nThis link expires in 24 hours.",
                    )
                    return self.send_json({"ok": True, "message": "Check your email for an account verification link."}, 201)
                if route.path == "/api/auth/verify":
                    token = str(payload.get("token", ""))
                    if not auth.verify_email(db, token):
                        return self.send_json({"error": "This verification link is invalid, expired, or already used."}, 400)
                    return self.send_json({"ok": True, "message": "Email verified. You can now sign in."})
                if route.path == "/api/auth/login":
                    try:
                        user = auth.authenticate_password(
                            db, str(payload.get("email", "")), str(payload.get("password", ""))
                        )
                    except auth.AuthenticationError as error:
                        return self.send_json({"error": str(error)}, 401)
                    credentials = auth.issue_session(db, user["id"])
                    self.set_auth_cookies(credentials)
                    return self.send_json({"user": user, "csrf_token": credentials.csrf_token})
                if route.path == "/api/auth/password-reset/request":
                    email_config = self.email_configuration()
                    if email_config is None:
                        return self.send_json({"error": "Password recovery is not configured yet."}, 503)
                    token = auth.create_password_reset_token(db, str(payload.get("email", "")))
                    if token:
                        user = db.execute(
                            "SELECT display_name FROM users WHERE email = %s",
                            (str(payload["email"]).strip().lower(),),
                        ).fetchone()
                        reset_url = f"{email_config['public_url']}/?reset={quote(token)}"
                        self.send_auth_email(
                            str(payload["email"]).strip(),
                            "Reset your OpenLead password",
                            f"Hello {user['display_name']}, reset your password using this link:\n\n{reset_url}\n\nThis link expires in one hour.",
                        )
                    return self.send_json({"ok": True, "message": "If that account exists, password reset instructions will arrive by email."})
                if route.path == "/api/auth/password-reset/complete":
                    token = str(payload.get("token", ""))
                    if not auth.reset_password(db, token, str(payload.get("password", ""))):
                        return self.send_json({"error": "This password reset link is invalid, expired, or already used."}, 400)
                    return self.send_json({"ok": True, "message": "Password reset. You can now sign in."})

                session = self.current_session(db)
                if route.path == "/api/auth/logout":
                    if not session:
                        self.clear_auth_cookies()
                        return self.send_json({"ok": True})
                    if not self.require_csrf(session):
                        return self.send_json({"error": "A valid CSRF token is required."}, 403)
                    auth.revoke_session(db, self.auth_cookie())
                    self.clear_auth_cookies()
                    return self.send_json({"ok": True})
                if not session:
                    return self.send_json({"error": "Sign in to access this workspace."}, 401)
                if not self.require_csrf(session):
                    return self.send_json({"error": "A valid CSRF token is required."}, 403)
                workspace_id = session["workspace_id"]
                settings = database.get_settings(db, workspace_id)
                if route.path == "/api/leads":
                    lead = database.create_lead(db, workspace_id, payload)
                    sync_ok, sync_detail = True, "CRM sync not configured."
                    if settings["crm_leads_url"]:
                        sync_ok, sync_detail = post_json(
                            settings["crm_leads_url"], lead_payload(lead), settings["crm_token"]
                        )
                        database.record_activity(
                            db, workspace_id, "crm" if sync_ok else "warning",
                            "Lead sent to connected CRM." if sync_ok else f"CRM sync failed: {sync_detail}",
                            lead["id"],
                        )
                    return self.send_json({"lead": lead, "crm_sync": sync_detail}, 201)
                if route.path == "/api/settings":
                    database.save_settings(db, workspace_id, payload)
                    return self.send_json(public_settings(database.get_settings(db, workspace_id)))
                if route.path == "/api/agent/chat":
                    message = str(payload.get("message", "")).strip()
                    if not message:
                        raise ValueError("Enter a question for the assistant.")
                    leads = database.list_leads(db, workspace_id, limit=30)
                    try:
                        answer = public_llm_chat(settings, [
                            {"role": "system", "content": "You are an operations assistant for a real-estate lead acquisition team. Use only the provided CRM snapshot, clearly say when data is missing, and never claim to perform actions."},
                            {"role": "user", "content": f"CRM snapshot: {json.dumps(leads)}\n\nQuestion: {message}"},
                        ])
                        return self.send_json({"answer": answer, "provider": settings["ai_provider"]})
                    except (
                        OSError,
                        TimeoutError,
                        ValueError,
                        KeyError,
                        IndexError,
                        TypeError,
                        urllib.error.URLError,
                        http.client.HTTPException,
                    ) as error:
                        print(f"OpenLead LLM request failed: {type(error).__name__}")
                        return self.send_json({"error": "The configured LLM request failed. Check provider, model, API key, and account quota."}, 503)
                match = re.fullmatch(r"/api/leads/(\d+)/qualify", route.path)
                if match:
                    lead_id = int(match.group(1))
                    row = database.get_lead(db, workspace_id, lead_id)
                    if not row:
                        return self.send_json({"error": "Lead not found"}, 404)
                    result = qualify_lead(row, settings)
                    stage = "qualified" if result["score"] >= 65 else "nurture"
                    updated_lead = database.save_qualification(db, workspace_id, lead_id, result, stage)
                    if not updated_lead:
                        return self.send_json({"error": "Lead not found"}, 404)
                    followup_sent = False
                    if stage == "qualified" and row["consent_sms"] and settings["auto_followup_enabled"] == "true" and settings["sms_webhook_url"]:
                        ok, detail = post_json(settings["sms_webhook_url"], {
                            "event": "lead.qualified", "channel": "sms", "lead": lead_payload(updated_lead),
                            "message": f"Hi {row['name']}, thanks for reaching out. When is a good time to talk about your goals?",
                        })
                        followup_sent = ok
                        database.record_activity(
                            db, workspace_id, "sms" if ok else "warning",
                            "Automated qualification follow-up sent." if ok else f"Follow-up failed: {detail}",
                            lead_id,
                        )
                    return self.send_json({"lead": updated_lead, "qualification": result, "followup_sent": followup_sent})
                match = re.fullmatch(r"/api/leads/(\d+)/stage", route.path)
                if match:
                    lead_id = int(match.group(1))
                    stage = str(payload.get("stage", ""))
                    name = database.update_lead_stage(db, workspace_id, lead_id, stage)
                    if not name:
                        return self.send_json({"error": "Lead not found"}, 404)
                    return self.send_json({"ok": True, "stage": stage})
                match = re.fullmatch(r"/api/leads/(\d+)/(sms|call)", route.path)
                if match:
                    lead_id, channel = int(match.group(1)), match.group(2)
                    lead = database.get_lead(db, workspace_id, lead_id)
                    if not lead:
                        return self.send_json({"error": "Lead not found"}, 404)
                    consent_field = "consent_sms" if channel == "sms" else "consent_call"
                    endpoint_field = "sms_webhook_url" if channel == "sms" else "voice_webhook_url"
                    if not lead[consent_field]:
                        return self.send_json({"error": f"Recorded {channel.upper()} consent is required."}, 409)
                    if not settings[endpoint_field]:
                        return self.send_json({"error": f"Configure a {channel.upper()} webhook before sending."}, 409)
                    message = str(payload.get("message", "")).strip()
                    if channel == "sms" and not message:
                        raise ValueError("Enter a text message.")
                    ok, detail = post_json(settings[endpoint_field], {
                        "event": f"lead.outbound_{channel}", "channel": channel,
                        "lead": lead_payload(lead), "message": message,
                    })
                    database.record_activity(
                        db, workspace_id, channel if ok else "warning",
                        f"Outbound {channel.upper()} accepted by webhook." if ok else f"Outbound {channel.upper()} failed: {detail}",
                        lead_id,
                    )
                    return self.send_json({"ok": ok, "detail": detail}, 200 if ok else 502)
                if route.path == "/api/appointments":
                    lead_id = int(payload.get("lead_id", 0))
                    starts_at = str(payload.get("starts_at", "")).strip()
                    lead = database.get_lead(db, workspace_id, lead_id)
                    if not lead:
                        return self.send_json({"error": "Choose an existing lead."}, 404)
                    try:
                        starts_at_value = datetime.fromisoformat(starts_at)
                    except ValueError as error:
                        raise ValueError("Enter a valid appointment date and time.") from error
                    if starts_at_value.tzinfo is None:
                        starts_at_value = starts_at_value.replace(tzinfo=timezone.utc)
                    kind = str(payload.get("kind", "Consultation")).strip() or "Consultation"
                    appointment_id = database.create_appointment(
                        db, workspace_id, lead_id, starts_at_value, kind
                    )
                    if appointment_id is None:
                        return self.send_json({"error": "Choose an existing lead."}, 404)
                    if settings["calendar_webhook_url"]:
                        ok, detail = post_json(settings["calendar_webhook_url"], {
                            "event": "appointment.created", "appointment_id": appointment_id,
                            "starts_at": starts_at_value.isoformat(), "kind": kind,
                            "lead": lead_payload(dict(lead)),
                        })
                        if not ok:
                            database.record_activity(
                                db, workspace_id, "warning", f"Calendar sync failed: {detail}", lead_id
                            )
                    return self.send_json({"ok": True, "id": appointment_id}, 201)
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json({"error": str(error)}, 400)
        except auth.EmailDeliveryError as error:
            print(f"OpenLead email delivery failed: {error}")
            return self.send_json({"error": str(error)}, 503)
        except RuntimeError as error:
            return self.send_json({"error": str(error)}, 503)
        except psycopg.Error as error:
            print(f"OpenLead database operation failed: {error}")
            return self.send_json({"error": "OpenLead account storage is temporarily unavailable."}, 503)
        return self.send_json({"error": "Not found"}, 404)

    def send_file(self, name: str, content_type: str) -> None:
        path = (STATIC / name).resolve()
        if path.parent != STATIC.resolve() or not path.is_file():
            return self.send_json({"error": "Not found"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Open Lead Agent running at http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()