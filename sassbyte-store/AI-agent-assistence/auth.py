from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from uuid import uuid4


PASSWORD_SCRYPT_N = 2**15
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
PASSWORD_SCRYPT_MAXMEM = 128 * 1024 * 1024
SESSION_COOKIE = "openlead_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_LIFETIME = timedelta(days=14)
EMAIL_TOKEN_LIFETIME = timedelta(hours=24)
RESET_TOKEN_LIFETIME = timedelta(hours=1)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_LOCKOUT = timedelta(minutes=15)
LOGIN_FAILURE_LIMIT = 5
DUMMY_PASSWORD_SALT = b"openlead-password-verification"
DUMMY_PASSWORD_HASH = (
    "scrypt$32768$8$1$"
    "d5465cb8e0981cb8b6d4ea3cde7043b1086f52fb8d39222ec700df9d438b0ef4"
    "a3f997cea402b44e2a96b092668adea034efef46c2e651ab0a57d365c87b9129"
)
EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class AuthenticationError(ValueError):
    pass


class EmailDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionCredentials:
    token: str
    csrf_token: str
    expires_at: datetime


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, bytes]:
    if len(password) < 14 or len(password) > 256:
        raise ValueError("Password must be between 14 and 256 characters.")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R,
        p=PASSWORD_SCRYPT_P,
        dklen=64,
        maxmem=PASSWORD_SCRYPT_MAXMEM,
    )
    encoded = f"scrypt${PASSWORD_SCRYPT_N}${PASSWORD_SCRYPT_R}${PASSWORD_SCRYPT_P}${digest.hex()}"
    return encoded, salt


def verify_password(password: str, encoded_hash: str | None, salt: bytes | None) -> bool:
    candidate_hash = encoded_hash or DUMMY_PASSWORD_HASH
    candidate_salt = salt or DUMMY_PASSWORD_SALT
    try:
        algorithm, n_value, r_value, p_value, digest = candidate_hash.split("$", 4)
        if algorithm != "scrypt":
            return False
        n, r, p = int(n_value), int(r_value), int(p_value)
        if n != PASSWORD_SCRYPT_N or r != PASSWORD_SCRYPT_R or p != PASSWORD_SCRYPT_P:
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=candidate_salt,
            n=n,
            r=r,
            p=p,
            dklen=64,
            maxmem=PASSWORD_SCRYPT_MAXMEM,
        ).hex()
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(candidate, digest)


def digest_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def validate_email(email: str) -> str:
    normalized = email.strip().lower()
    if len(normalized) > 254 or not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("Enter a valid email address.")
    return normalized


def create_account(
    db: Any,
    email: str,
    display_name: str,
    password: str,
) -> tuple[int, str]:
    email = validate_email(email)
    display_name = display_name.strip()
    if not display_name or len(display_name) > 120:
        raise ValueError("Name must be between 1 and 120 characters.")
    encoded_password, salt = hash_password(password)
    verification_token = secrets.token_urlsafe(32)
    user = db.execute(
        """
        INSERT INTO users (email, display_name, password_hash, password_salt)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (email, display_name, encoded_password, salt),
    ).fetchone()
    user_id = user["id"]
    db.execute(
        "INSERT INTO workspaces (id, owner_user_id, name) VALUES (%s, %s, %s)",
        (uuid4(), user_id, f"{display_name}'s workspace"),
    )
    db.execute(
        """
        INSERT INTO auth_tokens (token_digest, user_id, purpose, expires_at)
        VALUES (%s, %s, 'verify_email', %s)
        """,
        (digest_token(verification_token), user_id, datetime.now(timezone.utc) + EMAIL_TOKEN_LIFETIME),
    )
    return user_id, verification_token


def issue_session(db: Any, user_id: int) -> SessionCredentials:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME
    db.execute(
        """
        INSERT INTO auth_sessions (token_digest, csrf_digest, user_id, expires_at)
        VALUES (%s, %s, %s, %s)
        """,
        (digest_token(token), digest_token(csrf_token), user_id, expires_at),
    )
    return SessionCredentials(token, csrf_token, expires_at)


def resolve_session(db: Any, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = db.execute(
        """
        SELECT u.id AS user_id, u.email, u.display_name, w.id AS workspace_id,
               s.csrf_digest, s.expires_at
        FROM auth_sessions s
        JOIN users u ON u.id = s.user_id
        JOIN workspaces w ON w.owner_user_id = u.id
        WHERE s.token_digest = %s
          AND s.expires_at > now()
          AND u.email_verified_at IS NOT NULL
        """,
        (digest_token(token),),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def validate_csrf(session: dict[str, Any], supplied_token: str | None) -> bool:
    if not supplied_token:
        return False
    return hmac.compare_digest(bytes(session["csrf_digest"]), digest_token(supplied_token))


def revoke_session(db: Any, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM auth_sessions WHERE token_digest = %s", (digest_token(token),))


def authenticate_password(db: Any, email: str, password: str) -> dict[str, Any]:
    normalized_email = validate_email(email)
    email_hash = hashlib.sha256(normalized_email.encode("utf-8")).digest()
    attempt = db.execute(
        "SELECT failures, window_started_at, locked_until FROM auth_login_attempts WHERE email_hash = %s FOR UPDATE",
        (email_hash,),
    ).fetchone()
    now = datetime.now(timezone.utc)
    if attempt and attempt["locked_until"] and attempt["locked_until"] > now:
        raise AuthenticationError("Sign-in is temporarily unavailable. Try again later.")
    if attempt and attempt["window_started_at"] + LOGIN_WINDOW <= now:
        db.execute("DELETE FROM auth_login_attempts WHERE email_hash = %s", (email_hash,))
        attempt = None
    user = db.execute(
        """
        SELECT id, email, display_name, password_hash, password_salt, email_verified_at
        FROM users WHERE email = %s
        """,
        (normalized_email,),
    ).fetchone()
    stored_hash = user["password_hash"] if user else None
    stored_salt = bytes(user["password_salt"]) if user and user["password_salt"] else None
    password_matches = verify_password(password, stored_hash, stored_salt)
    if not user or not password_matches or user["email_verified_at"] is None:
        db.execute(
            """
            INSERT INTO auth_login_attempts (email_hash, failures, window_started_at, locked_until)
            VALUES (%s, 1, %s, NULL)
            ON CONFLICT (email_hash) DO UPDATE
            SET failures = CASE
                    WHEN auth_login_attempts.window_started_at + %s <= now() THEN 1
                    ELSE auth_login_attempts.failures + 1
                END,
                window_started_at = CASE
                    WHEN auth_login_attempts.window_started_at + %s <= now()
                    THEN EXCLUDED.window_started_at
                    ELSE auth_login_attempts.window_started_at
                END,
                locked_until = CASE
                    WHEN (CASE
                        WHEN auth_login_attempts.window_started_at + %s <= now() THEN 1
                        ELSE auth_login_attempts.failures + 1
                    END) >= %s THEN now() + %s
                    ELSE NULL
                END
            """,
            (
                email_hash, now, LOGIN_WINDOW, LOGIN_WINDOW, LOGIN_WINDOW,
                LOGIN_FAILURE_LIMIT, LOGIN_LOCKOUT,
            ),
        )
        raise AuthenticationError("Email or password is incorrect, or the email has not been verified.")
    db.execute("DELETE FROM auth_login_attempts WHERE email_hash = %s", (email_hash,))
    return {"id": user["id"], "email": user["email"], "display_name": user["display_name"]}


def consume_email_token(db: Any, token: str, purpose: str) -> int | None:
    if purpose not in {"verify_email", "reset_password"}:
        raise ValueError("Unsupported email token purpose.")
    row = db.execute(
        """
        UPDATE auth_tokens
        SET consumed_at = now()
        WHERE token_digest = %s
          AND purpose = %s
          AND consumed_at IS NULL
          AND expires_at > now()
        RETURNING user_id
        """,
        (digest_token(token), purpose),
    ).fetchone()
    return row["user_id"] if row else None


def verify_email(db: Any, token: str) -> bool:
    user_id = consume_email_token(db, token, "verify_email")
    if user_id is None:
        return False
    db.execute(
        "UPDATE users SET email_verified_at = now() WHERE id = %s AND email_verified_at IS NULL",
        (user_id,),
    )
    return True


def create_password_reset_token(db: Any, email: str) -> str | None:
    normalized_email = validate_email(email)
    user = db.execute(
        "SELECT id FROM users WHERE email = %s AND email_verified_at IS NOT NULL",
        (normalized_email,),
    ).fetchone()
    if not user:
        return None
    token = secrets.token_urlsafe(32)
    db.execute(
        """
        INSERT INTO auth_tokens (token_digest, user_id, purpose, expires_at)
        VALUES (%s, %s, 'reset_password', %s)
        """,
        (digest_token(token), user["id"], datetime.now(timezone.utc) + RESET_TOKEN_LIFETIME),
    )
    return token


def reset_password(db: Any, token: str, password: str) -> bool:
    encoded_password, salt = hash_password(password)
    user = db.execute(
        """
        WITH consumed AS (
            UPDATE auth_tokens
            SET consumed_at = now()
            WHERE token_digest = %s
              AND purpose = 'reset_password'
              AND consumed_at IS NULL
              AND expires_at > now()
            RETURNING user_id
        )
        UPDATE users
        SET password_hash = %s, password_salt = %s
        WHERE id = (SELECT user_id FROM consumed)
          AND email_verified_at IS NOT NULL
        RETURNING id
        """,
        (digest_token(token), encoded_password, salt),
    ).fetchone()
    if not user:
        return False
    db.execute("DELETE FROM auth_sessions WHERE user_id = %s", (user["id"],))
    return True


def create_oauth_account(db: Any, email: str, display_name: str, provider: str, subject: str) -> int:
    email = validate_email(email)
    display_name = display_name.strip()
    if provider != "google" or not subject or len(subject) > 255:
        raise ValueError("Unsupported OAuth identity.")
    if not display_name or len(display_name) > 120:
        raise ValueError("Name must be between 1 and 120 characters.")
    if db.execute(
        "SELECT 1 FROM users WHERE email = %s", (email,)
    ).fetchone():
        raise ValueError("This email already has an account; sign in with its existing method.")
    user = db.execute(
        """
        INSERT INTO users (email, display_name, email_verified_at)
        VALUES (%s, %s, now())
        RETURNING id
        """,
        (email, display_name),
    ).fetchone()
    user_id = user["id"]
    db.execute(
        "INSERT INTO workspaces (id, owner_user_id, name) VALUES (%s, %s, %s)",
        (uuid4(), user_id, f"{display_name}'s workspace"),
    )
    db.execute(
        "INSERT INTO oauth_identities (provider, subject, user_id) VALUES (%s, %s, %s)",
        (provider, subject, user_id),
    )
    return user_id


def authenticate_google_identity(
    db: Any, email: str, display_name: str, subject: str
) -> dict[str, Any]:
    email = validate_email(email)
    subject = subject.strip()
    if not subject or len(subject) > 255:
        raise ValueError("Invalid Google account identity.")
    identity = db.execute(
        """
        SELECT u.id, u.email, u.display_name
        FROM oauth_identities i
        JOIN users u ON u.id = i.user_id
        WHERE i.provider = 'google' AND i.subject = %s
          AND u.email_verified_at IS NOT NULL
        """,
        (subject,),
    ).fetchone()
    if identity:
        return {
            "id": identity["id"],
            "email": identity["email"],
            "display_name": identity["display_name"],
        }
    user_id = create_oauth_account(db, email, display_name, "google", subject)
    return {"id": user_id, "email": email, "display_name": display_name.strip()}


def deliver_email(
    recipient: str,
    subject: str,
    body: str,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    sender: str,
) -> None:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise EmailDeliveryError("Email delivery failed; check the SMTP configuration.") from error
