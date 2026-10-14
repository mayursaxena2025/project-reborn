from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import secrets
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "migrations" / "001_private_workspaces.sql"
IMPORT_KEY = "sqlite_owner_import_v1"
SETTING_SECRET_KEYS = {"crm_token", "webhook_key"}
PASSWORD_SCRYPT_N = 2**15
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1


def hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R,
        p=PASSWORD_SCRYPT_P,
        dklen=64,
        maxmem=128 * 1024 * 1024,
    )
    return (
        f"scrypt${PASSWORD_SCRYPT_N}${PASSWORD_SCRYPT_R}${PASSWORD_SCRYPT_P}$"
        f"{digest.hex()}"
    )


def read_legacy_rows(connection: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> list[dict]:
    available = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    missing = set(columns) - available
    if missing:
        raise ValueError(f"Legacy table {table} is missing columns: {', '.join(sorted(missing))}")
    names = ", ".join(f'"{column}"' for column in columns)
    return [dict(zip(columns, row)) for row in connection.execute(f'SELECT {names} FROM "{table}"')]


def import_legacy_data(
    database_url: str,
    sqlite_path: Path,
    owner_email: str,
    owner_name: str,
    password: str,
    encryption_key: bytes,
) -> None:
    sqlite_path = sqlite_path.resolve()
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"Legacy SQLite database does not exist: {sqlite_path}")
    owner_email = owner_email.strip().lower()
    owner_name = owner_name.strip()
    if len(owner_email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", owner_email):
        raise ValueError("Provide a valid owner email.")
    if not owner_name or len(owner_name) > 120:
        raise ValueError("The owner name must be between 1 and 120 characters.")
    if len(password) < 14 or len(password) > 256:
        raise ValueError("The owner password must be between 14 and 256 characters.")
    fernet = Fernet(encryption_key)
    password_salt = secrets.token_bytes(16)
    workspace_id = uuid.uuid4()
    verified_at = datetime.now(timezone.utc)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as legacy:
        legacy.row_factory = sqlite3.Row
        leads = read_legacy_rows(
            legacy,
            "leads",
            (
                "id", "name", "phone", "email", "source", "timeline", "budget", "notes",
                "consent_sms", "consent_call", "stage", "score", "qualification", "crm_ref", "created_at",
            ),
        )
        appointments = read_legacy_rows(
            legacy, "appointments", ("id", "lead_id", "starts_at", "kind", "created_at")
        )
        activities = read_legacy_rows(
            legacy, "activities", ("id", "lead_id", "kind", "detail", "created_at")
        )
        settings = read_legacy_rows(legacy, "settings", ("key", "value"))

    with psycopg.connect(database_url, row_factory=dict_row) as db:
        for statement in schema_sql.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
            (SCHEMA_PATH.name,),
        )

        if db.execute(
            "SELECT 1 FROM data_migrations WHERE migration_key = %s", (IMPORT_KEY,)
        ).fetchone():
            raise ValueError(f"Migration {IMPORT_KEY} has already completed; refusing to import twice.")
        for table in (
            "users",
            "workspaces",
            "oauth_identities",
            "auth_sessions",
            "auth_tokens",
            "leads",
            "appointments",
            "activities",
            "workspace_settings",
            "integration_secrets",
            "integration_deliveries",
        ):
            if db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise ValueError(f"Target table {table} is not empty; refusing to mix legacy data.")

        owner = db.execute(
            """
            INSERT INTO users (email, display_name, password_hash, password_salt, email_verified_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (owner_email, owner_name, hash_password(password, password_salt), password_salt, verified_at),
        ).fetchone()
        user_id = owner["id"]
        workspace = db.execute(
            """
            INSERT INTO workspaces (id, owner_user_id, name)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (workspace_id, user_id, f"{owner_name}'s workspace"),
        ).fetchone()
        workspace_id = workspace["id"]

        for row in leads:
            qualification = row["qualification"] or "{}"
            db.execute(
                """
                INSERT INTO leads
                    (id, workspace_id, name, phone, email, source, timeline, budget, notes,
                     consent_sms, consent_call, stage, score, qualification, crm_ref, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    row["id"], workspace_id, row["name"], row["phone"], row["email"], row["source"],
                    row["timeline"], row["budget"], row["notes"], bool(row["consent_sms"]),
                    bool(row["consent_call"]), row["stage"], row["score"], qualification,
                    row["crm_ref"], row["created_at"],
                ),
            )

        for row in appointments:
            db.execute(
                """
                INSERT INTO appointments (id, workspace_id, lead_id, starts_at, kind, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (row["id"], workspace_id, row["lead_id"], row["starts_at"], row["kind"], row["created_at"]),
            )

        for row in activities:
            db.execute(
                """
                INSERT INTO activities (id, workspace_id, lead_id, kind, detail, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (row["id"], workspace_id, row["lead_id"], row["kind"], row["detail"], row["created_at"]),
            )

        for row in settings:
            if row["key"] in {"ollama_url", "ollama_model"}:
                continue
            if row["key"] in SETTING_SECRET_KEYS:
                if row["value"]:
                    db.execute(
                        """
                        INSERT INTO integration_secrets (workspace_id, key, ciphertext)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (workspace_id, key) DO NOTHING
                        """,
                        (workspace_id, row["key"], fernet.encrypt(row["value"].encode("utf-8"))),
                    )
            else:
                db.execute(
                    """
                    INSERT INTO workspace_settings (workspace_id, key, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (workspace_id, key) DO NOTHING
                    """,
                    (workspace_id, row["key"], row["value"]),
                )

        for table in ("leads", "appointments", "activities", "integration_deliveries"):
            maximum_id = db.execute(f"SELECT MAX(id) AS max_id FROM {table}").fetchone()["max_id"]
            db.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence(%s, 'id'),
                    %s,
                    %s
                )
                """,
                (table, maximum_id or 1, maximum_id is not None),
            )
        db.execute("INSERT INTO data_migrations (migration_key) VALUES (%s)", (IMPORT_KEY,))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a single-workspace OpenLead SQLite database into PostgreSQL."
    )
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--owner-name", required=True)
    args = parser.parse_args()
    database_url = os.environ.get("OPENLEAD_DATABASE_URL")
    encryption_key = os.environ.get("OPENLEAD_ENCRYPTION_KEY", "").encode("ascii")
    if not database_url:
        parser.error("Set OPENLEAD_DATABASE_URL in the environment.")
    if not encryption_key:
        parser.error("Set OPENLEAD_ENCRYPTION_KEY to a Fernet key stored outside PostgreSQL.")
    try:
        Fernet(encryption_key)
    except (ValueError, TypeError):
        parser.error("OPENLEAD_ENCRYPTION_KEY must be a valid Fernet key.")
    password = getpass.getpass("New owner password (14+ characters): ")
    confirmation = getpass.getpass("Confirm owner password: ")
    if password != confirmation:
        parser.error("Passwords do not match.")
    try:
        import_legacy_data(
            database_url,
            args.sqlite_path,
            args.owner_email,
            args.owner_name,
            password,
            encryption_key,
        )
    except (OSError, sqlite3.Error, psycopg.Error, ValueError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1
    print("Legacy OpenLead data imported into the designated owner workspace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
