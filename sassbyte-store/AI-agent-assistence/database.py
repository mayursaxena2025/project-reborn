from __future__ import annotations

import json
import os
import re
import secrets
import threading
import hashlib
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from integration_http import IntegrationEndpointError, resolve_public_https_target
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"
DEFAULTS = {
    "crm_leads_url": "",
    "sms_webhook_url": "",
    "voice_webhook_url": "",
    "calendar_webhook_url": "",
    "ai_provider": "",
    "ai_model": "",
    "auto_followup_enabled": "false",
}
AI_PROVIDERS = {"openai", "anthropic", "gemini"}
AI_SECRET_KEYS = {f"ai_api_key_{provider}" for provider in AI_PROVIDERS}
SECRET_KEYS = {"crm_token", "webhook_key"} | AI_SECRET_KEYS
SETTING_KEYS = set(DEFAULTS) | SECRET_KEYS | {"ai_api_key"}
STAGES = {"new", "qualified", "nurture", "appointment", "under_contract", "closed", "disqualified"}

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


class DatabaseNotConfigured(RuntimeError):
    pass


class IntegrationSecretsNotConfigured(RuntimeError):
    pass


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        database_url = os.environ.get("OPENLEAD_DATABASE_URL")
        if not database_url:
            raise DatabaseNotConfigured("OPENLEAD_DATABASE_URL is required for the PostgreSQL runtime.")
        minimum = int(os.environ.get("OPENLEAD_DB_POOL_MIN", "1"))
        maximum = int(os.environ.get("OPENLEAD_DB_POOL_MAX", "10"))
        if minimum < 1 or maximum < minimum or maximum > 100:
            raise ValueError("Database pool bounds must satisfy 1 <= min <= max <= 100.")
        pool = ConnectionPool(
            conninfo=database_url,
            min_size=minimum,
            max_size=maximum,
            timeout=10,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        try:
            pool.open(wait=True, timeout=15)
        except Exception:
            pool.close()
            raise
        _pool = pool
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def connection() -> Iterator[Any]:
    with get_pool().connection() as db:
        yield db


def initialize_database() -> None:
    migration_files = sorted(MIGRATIONS.glob("*.sql"))
    with connection() as db:
        with db.transaction():
            db.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       version TEXT PRIMARY KEY,
                       applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                   )"""
            )
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("openlead:schema-migrations",))
            for path in migration_files:
                applied = db.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s",
                    (path.name,),
                ).fetchone()
                if applied:
                    continue
                for statement in path.read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))


def workspace_for_user(db: Any, user_id: int) -> UUID:
    row = db.execute(
        "SELECT id FROM workspaces WHERE owner_user_id = %s",
        (user_id,),
    ).fetchone()
    if not row:
        raise LookupError("User workspace was not found.")
    return row["id"]


def create_google_oauth_state(db: Any) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    db.execute("DELETE FROM oauth_login_states WHERE expires_at <= now()")
    db.execute(
        """
        INSERT INTO oauth_login_states
            (state_digest, nonce_digest, pkce_challenge, expires_at)
        VALUES (%s, %s, %s, %s)
        """,
        (
            hashlib.sha256(state.encode("ascii")).digest(),
            hashlib.sha256(nonce.encode("ascii")).digest(),
            challenge,
            datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    )
    return state, nonce, verifier


def consume_google_oauth_state(
    db: Any, state: str, nonce: str, verifier: str
) -> bool:
    try:
        state_digest = hashlib.sha256(state.encode("ascii")).digest()
        nonce_digest = hashlib.sha256(nonce.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
    except UnicodeEncodeError:
        return False
    row = db.execute(
        """
        DELETE FROM oauth_login_states
        WHERE state_digest = %s
          AND nonce_digest = %s
          AND pkce_challenge = %s
          AND expires_at > now()
        RETURNING state_digest
        """,
        (state_digest, nonce_digest, challenge),
    ).fetchone()
    return row is not None


def _fernet() -> Fernet:
    key = os.environ.get("OPENLEAD_ENCRYPTION_KEY")
    if not key:
        raise IntegrationSecretsNotConfigured(
            "OPENLEAD_ENCRYPTION_KEY is required to read or save integration secrets."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise IntegrationSecretsNotConfigured("OPENLEAD_ENCRYPTION_KEY is invalid.") from error


def _serialize_lead(row: Any) -> dict[str, Any]:
    lead = dict(row)
    qualification = lead.get("qualification")
    if not isinstance(qualification, str):
        lead["qualification"] = json.dumps(qualification or {})
    for key in ("consent_sms", "consent_call"):
        lead[key] = bool(lead[key])
    return lead


def record_activity(
    db: Any,
    workspace_id: UUID,
    kind: str,
    detail: str,
    lead_id: int | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO activities (workspace_id, lead_id, kind, detail, created_at)
        VALUES (%s, %s, %s, %s, now())
        """,
        (workspace_id, lead_id, kind, detail),
    )


def get_settings(db: Any, workspace_id: UUID) -> dict[str, str]:
    values = dict(DEFAULTS)
    rows = db.execute(
        "SELECT key, value FROM workspace_settings WHERE workspace_id = %s",
        (workspace_id,),
    ).fetchall()
    values.update({row["key"]: row["value"] for row in rows})
    secrets = db.execute(
        "SELECT key, ciphertext FROM integration_secrets WHERE workspace_id = %s",
        (workspace_id,),
    ).fetchall()
    if secrets:
        decrypt = _fernet()
        try:
            values.update(
                {row["key"]: decrypt.decrypt(bytes(row["ciphertext"])).decode("utf-8") for row in secrets}
            )
        except (InvalidToken, UnicodeDecodeError) as error:
            raise IntegrationSecretsNotConfigured("A saved integration secret cannot be decrypted.") from error
    for key in SECRET_KEYS:
        values.setdefault(key, "")
    provider = values.get("ai_provider", "")
    values["ai_api_key"] = values.get(f"ai_api_key_{provider}", "") if provider in AI_PROVIDERS else ""
    return values


def save_settings(db: Any, workspace_id: UUID, updates: dict[str, Any]) -> None:
    unknown = set(updates) - SETTING_KEYS
    if unknown:
        raise ValueError(f"Unsupported setting: {sorted(unknown)[0]}")
    encrypted_values: dict[str, bytes] = {}
    secrets = {key: str(value).strip() for key, value in updates.items() if key in SECRET_KEYS}
    api_key = str(updates.get("ai_api_key", "")).strip()
    if api_key:
        provider = str(updates.get("ai_provider", "")).strip()
        if provider not in AI_PROVIDERS:
            raise ValueError("Select an AI provider before saving its API key.")
        secrets[f"ai_api_key_{provider}"] = api_key
    if any(secrets.values()):
        encrypt = _fernet()
        encrypted_values = {key: encrypt.encrypt(value.encode("utf-8")) for key, value in secrets.items() if value}
    for key, value in updates.items():
        if key == "ai_api_key" or key in SECRET_KEYS:
            continue
        if key == "auto_followup_enabled":
            value = "true" if value is True or str(value).lower() == "true" else "false"
        else:
            value = str(value).strip()
            if key in {"crm_leads_url", "sms_webhook_url", "voice_webhook_url", "calendar_webhook_url"} and value:
                try:
                    resolve_public_https_target(value)
                except IntegrationEndpointError as error:
                    raise ValueError(f"{key}: {error}") from error
            if key == "ai_provider" and value not in {"", "openai", "anthropic", "gemini"}:
                raise ValueError("Choose OpenAI, Anthropic, or Gemini as the AI provider.")
            if key == "ai_model" and len(value) > 120:
                raise ValueError("AI model names cannot exceed 120 characters.")
        db.execute(
            """
            INSERT INTO workspace_settings (workspace_id, key, value, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (workspace_id, key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (workspace_id, key, value),
        )
    for key, ciphertext in encrypted_values.items():
        db.execute(
            """
            INSERT INTO integration_secrets (workspace_id, key, ciphertext, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (workspace_id, key)
            DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = now()
            """,
            (workspace_id, key, ciphertext),
        )


def create_lead(db: Any, workspace_id: UUID, data: dict[str, Any]) -> dict[str, Any]:
    name = str(data.get("name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    email = str(data.get("email", "")).strip()
    if not name or not (phone or email):
        raise ValueError("A name and at least one contact method are required.")
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Enter a valid email address.")
    lead = db.execute(
        """
        INSERT INTO leads
            (workspace_id, name, phone, email, source, timeline, budget, notes,
             consent_sms, consent_call, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        RETURNING *
        """,
        (
            workspace_id,
            name,
            phone,
            email,
            str(data.get("source", "Manual")).strip() or "Manual",
            str(data.get("timeline", "")).strip(),
            str(data.get("budget", "")).strip(),
            str(data.get("notes", "")).strip(),
            data.get("consent_sms") is True,
            data.get("consent_call") is True,
        ),
    ).fetchone()
    record_activity(db, workspace_id, "lead", f"New lead captured from {data.get('source', 'Manual')}.", lead["id"])
    return _serialize_lead(lead)


def get_lead(db: Any, workspace_id: UUID, lead_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM leads WHERE workspace_id = %s AND id = %s",
        (workspace_id, lead_id),
    ).fetchone()
    return _serialize_lead(row) if row else None


def list_leads(db: Any, workspace_id: UUID, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    pattern = f"%{query.strip()}%"
    if query.strip():
        rows = db.execute(
            """
            SELECT * FROM leads
            WHERE workspace_id = %s AND (name ILIKE %s OR phone ILIKE %s OR email ILIKE %s)
            ORDER BY created_at DESC, id DESC LIMIT %s
            """,
            (workspace_id, pattern, pattern, pattern, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM leads WHERE workspace_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
            (workspace_id, limit),
        ).fetchall()
    return [_serialize_lead(row) for row in rows]


def dashboard(db: Any, workspace_id: UUID) -> dict[str, Any]:
    counts = db.execute(
        "SELECT stage, count(*) AS amount FROM leads WHERE workspace_id = %s GROUP BY stage",
        (workspace_id,),
    ).fetchall()
    lead_count = db.execute(
        "SELECT count(*) AS amount FROM leads WHERE workspace_id = %s",
        (workspace_id,),
    ).fetchone()["amount"]
    appointments = db.execute(
        """
        SELECT a.id, a.starts_at, a.kind, l.name AS lead_name
        FROM appointments a
        JOIN leads l ON l.id = a.lead_id AND l.workspace_id = a.workspace_id
        WHERE a.workspace_id = %s AND a.starts_at >= now()
        ORDER BY a.starts_at LIMIT 6
        """,
        (workspace_id,),
    ).fetchall()
    return {
        "counts": {row["stage"]: row["amount"] for row in counts},
        "lead_count": lead_count,
        "appointments": [dict(row) for row in appointments],
    }


def list_activities(db: Any, workspace_id: UUID, limit: int = 30) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    rows = db.execute(
        """
        SELECT a.id, a.workspace_id, a.lead_id, a.kind, a.detail, a.created_at, l.name AS lead_name
        FROM activities a
        LEFT JOIN leads l ON l.id = a.lead_id AND l.workspace_id = a.workspace_id
        WHERE a.workspace_id = %s
        ORDER BY a.id DESC LIMIT %s
        """,
        (workspace_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def update_lead_stage(db: Any, workspace_id: UUID, lead_id: int, stage: str) -> str | None:
    if stage not in STAGES:
        raise ValueError("Choose a valid pipeline stage.")
    row = db.execute(
        """
        UPDATE leads SET stage = %s
        WHERE workspace_id = %s AND id = %s
        RETURNING name
        """,
        (stage, workspace_id, lead_id),
    ).fetchone()
    if not row:
        return None
    record_activity(db, workspace_id, "stage", f"{row['name']} moved to {stage.replace('_', ' ')}.", lead_id)
    return row["name"]


def save_qualification(
    db: Any,
    workspace_id: UUID,
    lead_id: int,
    result: dict[str, Any],
    stage: str,
) -> dict[str, Any] | None:
    if stage not in STAGES:
        raise ValueError("Choose a valid pipeline stage.")
    row = db.execute(
        """
        UPDATE leads SET score = %s, qualification = %s::jsonb, stage = %s
        WHERE workspace_id = %s AND id = %s
        RETURNING *
        """,
        (result["score"], json.dumps(result), stage, workspace_id, lead_id),
    ).fetchone()
    if not row:
        return None
    record_activity(
        db, workspace_id, "qualification",
        f"Qualified at {result['score']} points; moved to {stage}.", lead_id,
    )
    return _serialize_lead(row)


def create_appointment(
    db: Any,
    workspace_id: UUID,
    lead_id: int,
    starts_at: datetime,
    kind: str,
) -> int | None:
    appointment = db.execute(
        """
        INSERT INTO appointments (workspace_id, lead_id, starts_at, kind, created_at)
        SELECT %s, id, %s, %s, now()
        FROM leads
        WHERE workspace_id = %s AND id = %s
        RETURNING id
        """,
        (workspace_id, starts_at, kind, workspace_id, lead_id),
    ).fetchone()
    if not appointment:
        return None
    lead = db.execute(
        """
        UPDATE leads SET stage = 'appointment'
        WHERE workspace_id = %s AND id = %s
        RETURNING name
        """,
        (workspace_id, lead_id),
    ).fetchone()
    record_activity(
        db,
        workspace_id,
        "appointment",
        f"{kind} scheduled for {starts_at.isoformat()}.",
        lead_id,
    )
    return appointment["id"] if lead else None


def enqueue_delivery(db: Any, workspace_id: UUID, event_type: str, payload: dict[str, Any]) -> int:
    row = db.execute(
        """
        INSERT INTO integration_deliveries (workspace_id, event_type, payload)
        VALUES (%s, %s, %s::jsonb)
        RETURNING id
        """,
        (workspace_id, event_type, json.dumps(payload)),
    ).fetchone()
    return row["id"]
