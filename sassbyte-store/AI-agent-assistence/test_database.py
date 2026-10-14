import json
import os
import unittest
import base64
import hashlib
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet

import database


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeDatabase:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        return self.results.pop(0) if self.results else Result()


class WorkspaceDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.workspace_id = uuid4()

    def test_workspace_lookup_uses_authenticated_user_id(self):
        db = FakeDatabase([Result({"id": self.workspace_id})])
        self.assertEqual(database.workspace_for_user(db, 31), self.workspace_id)
        self.assertIn("owner_user_id = %s", db.calls[0][0])
        self.assertEqual(db.calls[0][1], (31,))

    def test_missing_workspace_fails_explicitly(self):
        with self.assertRaisesRegex(LookupError, "workspace was not found"):
            database.workspace_for_user(FakeDatabase([Result()]), 31)

    def test_google_oauth_state_persists_digests_and_pkce_challenge(self):
        db = FakeDatabase()
        state, nonce, verifier = database.create_google_oauth_state(db)
        self.assertIn("DELETE FROM oauth_login_states", db.calls[0][0])
        sql, parameters = db.calls[1]
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertIn("oauth_login_states", sql)
        self.assertEqual(parameters[0], hashlib.sha256(state.encode("ascii")).digest())
        self.assertEqual(parameters[1], hashlib.sha256(nonce.encode("ascii")).digest())
        self.assertEqual(parameters[2], expected_challenge)
        self.assertNotIn(state.encode(), parameters)
        self.assertNotIn(nonce.encode(), parameters)
        self.assertNotIn(verifier.encode(), parameters)

    def test_google_oauth_state_is_consumed_only_for_matching_nonce_and_verifier(self):
        db = FakeDatabase([Result({"state_digest": b"digest"})])
        state, nonce, verifier = "state-value", "nonce-value", "verifier-value"
        self.assertTrue(database.consume_google_oauth_state(db, state, nonce, verifier))
        self.assertIn("DELETE FROM oauth_login_states", db.calls[0][0])
        self.assertEqual(db.calls[0][1][0], hashlib.sha256(state.encode()).digest())
        self.assertFalse(
            database.consume_google_oauth_state(FakeDatabase([Result()]), state, nonce, "wrong-verifier")
        )

    def test_list_leads_always_scopes_to_workspace(self):
        db = FakeDatabase([Result(rows=[])])
        self.assertEqual(database.list_leads(db, self.workspace_id, "Jordan"), [])
        sql, parameters = db.calls[0]
        self.assertIn("workspace_id = %s", sql)
        self.assertIn("ILIKE", sql)
        self.assertEqual(parameters[0], self.workspace_id)
        self.assertEqual(parameters[-1], 100)

    def test_list_lead_limit_is_bounded(self):
        db = FakeDatabase([Result(rows=[])])
        database.list_leads(db, self.workspace_id, limit=5000)
        self.assertEqual(db.calls[0][1][-1], 100)

    def test_get_lead_scopes_id_and_workspace_and_serializes_fields(self):
        db = FakeDatabase([
            Result({
                "id": 2,
                "workspace_id": self.workspace_id,
                "qualification": {"score": 72},
                "consent_sms": True,
                "consent_call": False,
            })
        ])
        lead = database.get_lead(db, self.workspace_id, 2)
        sql, params = db.calls[0]
        self.assertIn("workspace_id = %s AND id = %s", sql)
        self.assertEqual(params, (self.workspace_id, 2))
        self.assertEqual(json.loads(lead["qualification"]), {"score": 72})
        self.assertIs(lead["consent_sms"], True)

    def test_dashboard_scopes_counts_and_appointments(self):
        db = FakeDatabase([
            Result(rows=[{"stage": "new", "amount": 2}]),
            Result({"amount": 2}),
            Result(rows=[]),
        ])
        snapshot = database.dashboard(db, self.workspace_id)
        self.assertEqual(snapshot["lead_count"], 2)
        self.assertEqual(len(db.calls), 3)
        self.assertTrue(all("workspace_id = %s" in sql for sql, _ in db.calls))
        self.assertTrue(all(parameters[0] == self.workspace_id for _, parameters in db.calls))

    def test_activity_join_is_tenant_scoped_on_both_sides(self):
        db = FakeDatabase([Result(rows=[])])
        database.list_activities(db, self.workspace_id)
        sql, parameters = db.calls[0]
        self.assertIn("a.workspace_id = %s", sql)
        self.assertIn("l.workspace_id = a.workspace_id", sql)
        self.assertEqual(parameters, (self.workspace_id, 30))

    def test_stage_update_cannot_modify_another_workspace(self):
        db = FakeDatabase([Result()])
        self.assertIsNone(database.update_lead_stage(db, self.workspace_id, 18, "qualified"))
        sql, parameters = db.calls[0]
        self.assertIn("WHERE workspace_id = %s AND id = %s", sql)
        self.assertEqual(parameters, ("qualified", self.workspace_id, 18))
        with self.assertRaisesRegex(ValueError, "valid pipeline stage"):
            database.update_lead_stage(db, self.workspace_id, 18, "arbitrary")

    def test_settings_are_workspace_scoped_and_secrets_decrypted(self):
        key = Fernet.generate_key().decode()
        encrypted = Fernet(key.encode()).encrypt(b"private-token")
        encrypted_llm_key = Fernet(key.encode()).encrypt(b"llm-private-key")
        db = FakeDatabase([
            Result(rows=[{"key": "ai_model", "value": "gpt-test"}, {"key": "ai_provider", "value": "openai"}]),
            Result(rows=[
                {"key": "crm_token", "ciphertext": encrypted},
                {"key": "ai_api_key_openai", "ciphertext": encrypted_llm_key},
            ]),
        ])
        with patch.dict(os.environ, {"OPENLEAD_ENCRYPTION_KEY": key}):
            settings = database.get_settings(db, self.workspace_id)
        self.assertEqual(settings["ai_model"], "gpt-test")
        self.assertEqual(settings["ai_provider"], "openai")
        self.assertEqual(settings["crm_token"], "private-token")
        self.assertEqual(settings["ai_api_key"], "llm-private-key")
        self.assertTrue(all("workspace_id = %s" in sql for sql, _ in db.calls))
        self.assertTrue(all(params == (self.workspace_id,) for _, params in db.calls))

    def test_settings_save_encrypts_tokens_and_preserves_other_settings(self):
        key = Fernet.generate_key().decode()
        db = FakeDatabase()
        with patch.dict(os.environ, {"OPENLEAD_ENCRYPTION_KEY": key}):
            database.save_settings(
                db,
                self.workspace_id,
                {
                    "crm_token": "private-token",
                    "ai_provider": "openai",
                    "ai_model": "gpt-test",
                    "ai_api_key": "llm-secret",
                },
            )
        secret_call = next(call for call in db.calls if "integration_secrets" in call[0])
        plain_setting_call = next(
            call for call in db.calls
            if "workspace_settings" in call[0] and call[1][1] == "ai_model"
        )
        secret_calls = [call for call in db.calls if "integration_secrets" in call[0]]
        encrypted_values = [call[1][2] for call in secret_calls]
        self.assertEqual(
            {Fernet(key.encode()).decrypt(value) for value in encrypted_values},
            {b"private-token", b"llm-secret"},
        )
        self.assertTrue(all(b"private-token" not in value and b"llm-secret" not in value for value in encrypted_values))
        self.assertEqual(
            plain_setting_call[1],
            (self.workspace_id, "ai_model", "gpt-test"),
        )
        self.assertTrue(all(self.workspace_id in params for _, params in db.calls))

    def test_unsupported_ai_provider_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Choose OpenAI"):
            database.save_settings(
                FakeDatabase(), self.workspace_id, {"ai_provider": "untrusted"}
            )

    def test_ai_keys_are_bound_to_provider_and_not_shared_when_switching(self):
        key = Fernet.generate_key().decode()
        save_db = FakeDatabase()
        with patch.dict(os.environ, {"OPENLEAD_ENCRYPTION_KEY": key}):
            database.save_settings(
                save_db,
                self.workspace_id,
                {"ai_provider": "openai", "ai_api_key": "openai-private-key"},
            )
        saved_key = next(call for call in save_db.calls if "integration_secrets" in call[0])
        self.assertEqual(saved_key[1][1], "ai_api_key_openai")
        self.assertNotIn(b"openai-private-key", saved_key[1][2])
        openai_saved = Fernet(key.encode()).encrypt(b"openai-private-key")
        read_db = FakeDatabase([
            Result(rows=[{"key": "ai_provider", "value": "anthropic"}]),
            Result(rows=[{"key": "ai_api_key_openai", "ciphertext": openai_saved}]),
        ])
        with patch.dict(os.environ, {"OPENLEAD_ENCRYPTION_KEY": key}):
            settings = database.get_settings(read_db, self.workspace_id)
        self.assertEqual(settings["ai_api_key"], "")

    def test_non_http_integration_endpoint_is_rejected(self):
        db = FakeDatabase()
        with self.assertRaisesRegex(ValueError, "public HTTPS"):
            database.save_settings(
                db, self.workspace_id, {"crm_leads_url": "file:///etc/passwd"}
            )
        self.assertEqual(db.calls, [])

    def test_private_webhook_ip_is_rejected_before_persistence(self):
        db = FakeDatabase()
        with self.assertRaisesRegex(ValueError, "private, local"):
            database.save_settings(
                db,
                self.workspace_id,
                {"sms_webhook_url": "https://127.0.0.1/webhook"},
            )
        self.assertEqual(db.calls, [])

    def test_plain_http_webhook_is_rejected_before_persistence(self):
        db = FakeDatabase()
        with self.assertRaisesRegex(ValueError, "public HTTPS"):
            database.save_settings(
                db,
                self.workspace_id,
                {"crm_leads_url": "http://crm.example/leads"},
            )
        self.assertEqual(db.calls, [])

    def test_create_lead_and_activity_share_workspace(self):
        lead_id = 13
        db = FakeDatabase([Result({"id": lead_id, "name": "Jordan", "consent_sms": True, "consent_call": False}), Result()])
        lead = database.create_lead(db, self.workspace_id, {"name": "Jordan", "phone": "555"})
        self.assertEqual(lead["id"], lead_id)
        self.assertEqual(len(db.calls), 2)
        self.assertEqual(db.calls[0][1][0], self.workspace_id)
        self.assertEqual(db.calls[1][1][0], self.workspace_id)

    def test_create_lead_rejects_missing_contact_before_database_write(self):
        db = FakeDatabase()
        with self.assertRaisesRegex(ValueError, "contact method"):
            database.create_lead(db, self.workspace_id, {"name": "Jordan"})
        self.assertEqual(db.calls, [])

    def test_pool_requires_explicit_database_url(self):
        with patch.object(database, "_pool", None), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(database.DatabaseNotConfigured, "OPENLEAD_DATABASE_URL"):
                database.get_pool()

    def test_initialize_database_applies_versioned_migrations_once(self):
        class Transaction:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        class MigrationDatabase(FakeDatabase):
            def __init__(self):
                super().__init__()
                self.applied = set()

            def transaction(self):
                return Transaction()

            def execute(self, statement, parameters=None):
                self.calls.append((statement, parameters))
                if "SELECT 1 FROM schema_migrations" in statement:
                    return Result({"version": parameters[0]} if parameters[0] in self.applied else None)
                if "INSERT INTO schema_migrations" in statement:
                    self.applied.add(parameters[0])
                return Result()

        db = MigrationDatabase()

        @contextmanager
        def fake_connection():
            yield db

        with patch.object(database, "connection", fake_connection):
            database.initialize_database()
            first_run_count = len(db.calls)
            database.initialize_database()

        self.assertIn("001_private_workspaces.sql", db.applied)
        self.assertEqual(
            sum("CREATE TABLE IF NOT EXISTS users" in statement for statement, _ in db.calls),
            1,
        )
        self.assertGreater(len(db.calls), first_run_count)


if __name__ == "__main__":
    unittest.main()
