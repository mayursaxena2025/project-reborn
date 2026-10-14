import os
import tempfile
import unittest

import main

sanitize_error_message = main.sanitize_error_message


class ErrorSanitizationTests(unittest.TestCase):
    def test_sanitize_error_message_hides_raw_auth_error(self):
        raw = "Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error', 'message': 'invalid x-api-key'}, 'request_id': 'req_011CfNcMG8urRN3WtsdFUmBE'}"

        cleaned = sanitize_error_message(raw)

        self.assertNotIn("invalid x-api-key", cleaned.lower())
        self.assertIn("Anthropic API key is invalid", cleaned)

    def test_use_env_credentials_prefers_saved_env_values(self):
        original_key = main.API_KEY
        original_workspace = main.WORKSPACE_ID

        try:
            main.API_KEY = "env-key"
            main.WORKSPACE_ID = "env-ws"
            selected_key, selected_workspace = main.resolve_credentials("", "", use_env=True)
            self.assertEqual(selected_key, "env-key")
            self.assertEqual(selected_workspace, "env-ws")
        finally:
            main.API_KEY = original_key
            main.WORKSPACE_ID = original_workspace

    def test_persist_storyboard_creates_unique_share_directory(self):
        deck_id = "share-test-123"
        saved_path = main.persist_storyboard(deck_id, {"name": "Test User", "slides": []})

        self.assertTrue(saved_path.endswith(deck_id))
        self.assertTrue(saved_path.startswith(main.PERSIST_BASE))
        self.assertTrue(main.os.path.exists(saved_path + "/deck.json"))

    def test_load_environment_uses_app_directory_not_cwd(self):
        original_cwd = os.getcwd()
        original_key = os.environ.pop("ANTHROPIC_API_KEY", None)

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                os.chdir(temp_dir)
                os.environ.pop("ANTHROPIC_API_KEY", None)
                main.load_environment()
                self.assertTrue(os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-"))
        finally:
            os.chdir(original_cwd)
            if original_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_key


if __name__ == "__main__":
    unittest.main()
