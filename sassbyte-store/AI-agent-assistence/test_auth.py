import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID

import auth


class AuthenticationPrimitiveTests(unittest.TestCase):
    def test_password_hash_verifies_and_salt_is_unique(self):
        password = "correct-horse-battery-staple"
        encoded, salt = auth.hash_password(password)
        other_encoded, other_salt = auth.hash_password(password)
        self.assertTrue(auth.verify_password(password, encoded, salt))
        self.assertFalse(auth.verify_password("wrong-password-long", encoded, salt))
        self.assertNotEqual(salt, other_salt)
        self.assertNotEqual(encoded, other_encoded)

    def test_missing_user_password_check_uses_valid_dummy_hash(self):
        self.assertFalse(auth.verify_password("wrong-password-long", None, None))

    def test_invalid_password_hash_format_is_rejected(self):
        self.assertFalse(auth.verify_password("a-valid-long-password", "pbkdf2$1", b"salt"))

    def test_password_length_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "14 and 256"):
            auth.hash_password("too-short")

    def test_email_is_normalized_and_validated(self):
        self.assertEqual(auth.validate_email(" User@Example.com "), "user@example.com")
        with self.assertRaisesRegex(ValueError, "valid email"):
            auth.validate_email("invalid")

    def test_token_digest_is_fixed_length_and_not_the_token(self):
        token = "random-secret-token-value"
        digest = auth.digest_token(token)
        self.assertEqual(len(digest), 32)
        self.assertNotEqual(digest, token.encode())

    def test_csrf_token_must_match_session_digest(self):
        csrf = "random-csrf-token"
        session = {"csrf_digest": auth.digest_token(csrf)}
        self.assertTrue(auth.validate_csrf(session, csrf))
        self.assertFalse(auth.validate_csrf(session, "different-token"))
        self.assertFalse(auth.validate_csrf(session, None))

    def test_account_creation_provisions_private_workspace_and_email_token(self):
        db = Mock()
        db.execute.return_value.fetchone.return_value = {"id": 23}

        user_id, verification_token = auth.create_account(
            db, " PERSON@example.com ", "Workspace Owner", "long-safe-password-value"
        )

        self.assertEqual(user_id, 23)
        self.assertEqual(len(verification_token), 43)
        statements = [call.args[0] for call in db.execute.call_args_list]
        self.assertTrue(any("INSERT INTO users" in statement for statement in statements))
        self.assertTrue(any("INSERT INTO workspaces" in statement for statement in statements))
        self.assertTrue(any("INSERT INTO auth_tokens" in statement for statement in statements))
        workspace_call = next(
            call for call in db.execute.call_args_list if "INSERT INTO workspaces" in call.args[0]
        )
        self.assertIsInstance(workspace_call.args[1][0], UUID)
        token_call = next(
            call for call in db.execute.call_args_list if "INSERT INTO auth_tokens" in call.args[0]
        )
        self.assertEqual(token_call.args[1][0], auth.digest_token(verification_token))

    def test_unverified_user_cannot_authenticate(self):
        db = Mock()
        encoded_hash, salt = auth.hash_password("long-safe-password-value")
        db.execute.side_effect = [
            _Result(None),
            _Result({
                "id": 7,
                "email": "user@example.com",
                "display_name": "User",
                "password_hash": encoded_hash,
                "password_salt": salt,
                "email_verified_at": None,
            }),
            _Result(None),
        ]
        with self.assertRaisesRegex(auth.AuthenticationError, "not been verified"):
            auth.authenticate_password(db, "user@example.com", "long-safe-password-value")

    def test_verified_user_authenticates_with_password(self):
        db = Mock()
        encoded_hash, salt = auth.hash_password("long-safe-password-value")
        db.execute.side_effect = [
            _Result(None),
            _Result({
                "id": 7,
                "email": "user@example.com",
                "display_name": "User",
                "password_hash": encoded_hash,
                "password_salt": salt,
                "email_verified_at": datetime.now(timezone.utc),
            }),
            _Result(None),
        ]
        user = auth.authenticate_password(db, "user@example.com", "long-safe-password-value")
        self.assertEqual(user["id"], 7)
        self.assertNotIn("password_hash", user)

    def test_failed_login_updates_persisted_rate_limit(self):
        db = Mock()
        db.execute.side_effect = [
            _Result(None),
            _Result(None),
            _Result(None),
        ]
        with self.assertRaises(auth.AuthenticationError):
            auth.authenticate_password(db, "missing@example.com", "wrong-password-long")
        rate_limit_update = next(
            call.args[0] for call in db.execute.call_args_list
            if "INSERT INTO auth_login_attempts" in call.args[0]
        )
        self.assertIn("auth_login_attempts.failures + 1", rate_limit_update)

    def test_password_reset_token_is_not_issued_for_unknown_email(self):
        db = Mock()
        db.execute.return_value.fetchone.return_value = None
        self.assertIsNone(auth.create_password_reset_token(db, "missing@example.com"))
        self.assertEqual(db.execute.call_count, 1)

    def test_password_reset_revokes_existing_sessions(self):
        db = Mock()
        db.execute.return_value.fetchone.return_value = {"id": 7}
        self.assertTrue(auth.reset_password(db, "one-time-reset-token", "replacement-password-long"))
        self.assertIn(
            "DELETE FROM auth_sessions WHERE user_id = %s",
            [call.args[0] for call in db.execute.call_args_list],
        )

    def test_expired_or_invalid_password_reset_token_does_not_revoke_sessions(self):
        db = Mock()
        db.execute.return_value.fetchone.return_value = None
        self.assertFalse(auth.reset_password(db, "invalid-reset-token", "replacement-password-long"))
        self.assertEqual(db.execute.call_count, 1)

    def test_oauth_account_provisions_a_verified_private_workspace(self):
        db = Mock()
        db.execute.side_effect = [
            _Result(None),
            _Result({"id": 29}),
            _Result(None),
            _Result(None),
        ]
        user_id = auth.create_oauth_account(
            db, "google@example.com", "Google User", "google", "provider-subject"
        )
        self.assertEqual(user_id, 29)
        statements = [call.args[0] for call in db.execute.call_args_list]
        self.assertTrue(any("INSERT INTO users" in statement for statement in statements))
        self.assertTrue(any("INSERT INTO workspaces" in statement for statement in statements))
        self.assertTrue(any("INSERT INTO oauth_identities" in statement for statement in statements))

    def test_oauth_cannot_silently_link_to_existing_email(self):
        db = Mock()
        db.execute.return_value.fetchone.return_value = {"id": 8}
        with self.assertRaisesRegex(ValueError, "already has an account"):
            auth.create_oauth_account(
                db, "existing@example.com", "Existing User", "google", "provider-subject"
            )
        self.assertEqual(db.execute.call_count, 1)

    def test_google_identity_authenticates_existing_provider_subject(self):
        db = Mock()
        db.execute.return_value.fetchone.return_value = {
            "id": 8,
            "email": "existing@example.com",
            "display_name": "Existing User",
        }
        user = auth.authenticate_google_identity(
            db, "new-email@example.com", "Google User", "google-subject"
        )
        self.assertEqual(user["id"], 8)
        self.assertEqual(user["email"], "existing@example.com")
        self.assertIn("i.provider = 'google'", db.execute.call_args.args[0])
        self.assertEqual(db.execute.call_count, 1)

    def test_google_identity_does_not_implicitly_link_by_email(self):
        db = Mock()
        db.execute.side_effect = [
            _Result(None),
            _Result({"id": 9}),
        ]
        with self.assertRaisesRegex(ValueError, "already has an account"):
            auth.authenticate_google_identity(
                db, "existing@example.com", "Google User", "google-subject"
            )
        self.assertEqual(db.execute.call_count, 2)


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


if __name__ == "__main__":
    unittest.main()
