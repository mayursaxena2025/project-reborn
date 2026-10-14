import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

import migrate_sqlite_to_postgres as migration


class FakePostgres:
    def __init__(self):
        self.statements = []
        self.migration_complete = False
        self.occupied_table = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if "SELECT 1 FROM data_migrations" in statement:
            return FakeResult({"ready": 1} if self.migration_complete else None)
        if statement.strip().startswith("SELECT 1 FROM"):
            table = statement.strip().split()[3]
            return FakeResult({"existing": 1} if table == self.occupied_table else None)
        if "INSERT INTO users" in statement:
            return FakeResult({"id": 17})
        if "INSERT INTO workspaces" in statement:
            return FakeResult({"id": parameters[0]})
        if "SELECT MAX(id)" in statement:
            return FakeResult({"max_id": None})
        if "INSERT INTO data_migrations" in statement:
            self.migration_complete = True
        return FakeResult(None)


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class PostgresMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.sqlite_path = Path(self.tempdir.name) / "legacy.sqlite3"
        with sqlite3.connect(self.sqlite_path) as db:
            db.executescript(
                """
                CREATE TABLE leads (
                    id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, source TEXT,
                    timeline TEXT, budget TEXT, notes TEXT, consent_sms INTEGER, consent_call INTEGER,
                    stage TEXT, score INTEGER, qualification TEXT, crm_ref TEXT, created_at TEXT
                );
                CREATE TABLE appointments (
                    id INTEGER PRIMARY KEY, lead_id INTEGER, starts_at TEXT, kind TEXT, created_at TEXT
                );
                CREATE TABLE activities (
                    id INTEGER PRIMARY KEY, lead_id INTEGER, kind TEXT, detail TEXT, created_at TEXT
                );
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO leads VALUES (
                    5, 'Jordan Lee', '555-0100', 'jordan@example.com', 'Manual',
                    '', '', '', 1, 0, 'new', NULL, '{}', '', '2026-01-01T00:00:00+00:00'
                );
                INSERT INTO appointments VALUES (
                    8, 5, '2026-01-03T12:00:00+00:00', 'Consultation', '2026-01-01T00:00:00+00:00'
                );
                INSERT INTO activities VALUES (
                    9, 5, 'lead', 'New lead captured.', '2026-01-01T00:00:00+00:00'
                );
                INSERT INTO settings VALUES ('crm_leads_url', 'https://crm.example/leads');
                INSERT INTO settings VALUES ('crm_token', 'integration-secret');
                """
            )
        self.pg = FakePostgres()

    def tearDown(self):
        self.tempdir.cleanup()

    def run_import(self, email="owner@example.com"):
        with patch.object(migration.psycopg, "connect", return_value=self.pg):
            migration.import_legacy_data(
                "postgresql://test",
                self.sqlite_path,
                email,
                "Workspace Owner",
                "safe-owner-password-long",
                Fernet.generate_key(),
            )

    def test_import_assigns_legacy_records_to_owner_workspace(self):
        self.run_import()
        inserts = [statement for statement, _ in self.pg.statements]
        self.assertTrue(any("INSERT INTO users" in statement for statement in inserts))
        self.assertTrue(any("INSERT INTO workspaces" in statement for statement in inserts))
        self.assertTrue(any("INSERT INTO leads" in statement for statement in inserts))
        self.assertTrue(any("INSERT INTO appointments" in statement for statement in inserts))
        self.assertTrue(any("INSERT INTO activities" in statement for statement in inserts))
        self.assertTrue(any("INSERT INTO data_migrations" in statement for statement in inserts))
        lead_insert = next(item for item in self.pg.statements if "INSERT INTO leads" in item[0])
        self.assertEqual(lead_insert[1][0], 5)
        self.assertEqual(lead_insert[1][1], next(item[1][0] for item in self.pg.statements if "INSERT INTO workspaces" in item[0]))

    def test_integration_secret_is_encrypted_before_insert(self):
        key = Fernet.generate_key()
        with patch.object(migration.psycopg, "connect", return_value=self.pg):
            migration.import_legacy_data(
                "postgresql://test",
                self.sqlite_path,
                "owner@example.com",
                "Workspace Owner",
                "safe-owner-password-long",
                key,
            )
        secret = next(item for item in self.pg.statements if "INSERT INTO integration_secrets" in item[0])
        self.assertNotEqual(secret[1][2], b"integration-secret")
        self.assertEqual(Fernet(key).decrypt(secret[1][2]), b"integration-secret")

    def test_legacy_ollama_settings_are_not_imported(self):
        with sqlite3.connect(self.sqlite_path) as legacy:
            legacy.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                [
                    ("ollama_url", "http://localhost:11434"),
                    ("ollama_model", "local-model"),
                ],
            )
        self.run_import()
        imported_settings = [
            parameters[1]
            for statement, parameters in self.pg.statements
            if "INSERT INTO workspace_settings" in statement
        ]
        self.assertNotIn("ollama_url", imported_settings)
        self.assertNotIn("ollama_model", imported_settings)

    def test_import_fails_on_invalid_owner_email_before_connecting(self):
        with patch.object(migration.psycopg, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "valid owner email"):
                migration.import_legacy_data(
                    "postgresql://test",
                    self.sqlite_path,
                    "not-an-email",
                    "Workspace Owner",
                    "safe-owner-password-long",
                    Fernet.generate_key(),
                )
        connect.assert_not_called()

    def test_reimport_is_rejected(self):
        self.pg.migration_complete = True
        with patch.object(migration.psycopg, "connect", return_value=self.pg):
            with self.assertRaisesRegex(ValueError, "already completed"):
                migration.import_legacy_data(
                    "postgresql://test",
                    self.sqlite_path,
                    "owner@example.com",
                    "Workspace Owner",
                    "safe-owner-password-long",
                    Fernet.generate_key(),
                )

    def test_schema_enforces_workspace_owned_records_and_secrets(self):
        schema = migration.SCHEMA_PATH.read_text(encoding="utf-8")
        self.assertIn("workspace_id UUID NOT NULL REFERENCES workspaces", schema)
        self.assertIn("FOREIGN KEY (lead_id, workspace_id)", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS integration_secrets", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS integration_deliveries", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS auth_sessions", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS oauth_identities", schema)

    def test_workspace_id_is_uuid(self):
        self.run_import()
        workspace_insert = next(item for item in self.pg.statements if "INSERT INTO workspaces" in item[0])
        self.assertIsInstance(workspace_insert[1][0], uuid.UUID)

    def test_import_refuses_to_mix_existing_target_data(self):
        self.pg.occupied_table = "leads"
        with patch.object(migration.psycopg, "connect", return_value=self.pg):
            with self.assertRaisesRegex(ValueError, "not empty"):
                migration.import_legacy_data(
                    "postgresql://test",
                    self.sqlite_path,
                    "owner@example.com",
                    "Workspace Owner",
                    "safe-owner-password-long",
                    Fernet.generate_key(),
                )
        self.assertFalse(any("INSERT INTO users" in statement for statement, _ in self.pg.statements))

    def test_owner_password_hash_encodes_scrypt_parameters(self):
        password_hash = migration.hash_password("safe-owner-password-long", b"0123456789abcdef")
        self.assertRegex(password_hash, r"^scrypt\$32768\$8\$1\$[0-9a-f]{128}$")


if __name__ == "__main__":
    unittest.main()
