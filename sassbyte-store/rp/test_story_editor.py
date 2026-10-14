import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

import main


class StoryEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_persist_base = main.PERSIST_BASE
        self.original_llm_json = main.llm_json
        main.PERSIST_BASE = self.temp_dir.name
        self.deck_id = "story-editor-test"
        self.token = "test-owner-token"
        self.deck = {
            "name": "Candidate",
            "gender": "N",
            "slides": [{"t": "title", "h": "Career story", "s": "Platform engineering"}],
            "profile": {
                "facts": [{
                    "id": "resume-fact",
                    "category": "experience",
                    "text": "Platform engineer",
                    "source": "resume",
                    "verified": True,
                }],
                "gaps": [{"category": "achievement", "question": "What changed?"}],
                "insights": [],
            },
        }
        main.persist_storyboard(self.deck_id, self.deck)
        main.persist_edit_token(self.deck_id, self.token)
        main.DECKS[self.deck_id] = (main.time.time() + main.TTL, self.deck)
        self.client = TestClient(main.app)

    def tearDown(self):
        main.DECKS.pop(self.deck_id, None)
        main.PERSIST_BASE = self.original_persist_base
        main.llm_json = self.original_llm_json
        self.temp_dir.cleanup()

    def test_interview_requires_private_edit_token(self):
        self.assertEqual(
            self.client.post(
                f"/api/deck/{self.deck_id}/interview",
                json={"message": "I led six engineers."},
            ).status_code,
            403,
        )

        def fake_llm(system, messages, max_tokens=3000):
            return {
                "reply": "Thanks. Would you like to add that to your presentation?",
                "proposals": [{
                    "category": "leadership",
                    "text": "Led six engineers.",
                    "presentation_section": "Leadership",
                }],
                "insights": [],
            }

        main.llm_json = fake_llm
        response = self.client.post(
            f"/api/deck/{self.deck_id}/interview",
            headers={"X-Story-Editor-Token": self.token},
            json={"message": "I led six engineers.", "history": []},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["proposals"][0]["text"], "Led six engineers.")

    def test_approved_fact_updates_story_and_stays_out_of_share_html(self):
        def fake_llm(system, messages, max_tokens=3000):
            return {
                "name": "Generated name",
                "gender": "N",
                "slides": [{
                    "t": "title",
                    "h": "Career story",
                    "s": "Platform engineering",
                    "n": "A career in platform engineering.",
                    "fact_ids": [],
                }],
            }

        main.llm_json = fake_llm
        response = self.client.post(
            f"/api/deck/{self.deck_id}/story-facts",
            headers={"X-Story-Editor-Token": self.token},
            json={
                "category": "achievement",
                "text": "<script>alert(1)</script> reduced deploys from 40 to 8 minutes",
                "presentation_section": "Delivery impact",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(any(
            fact["source"] == "user_chat" and "<script>alert(1)</script>" in fact["text"]
            for fact in result["deck"]["profile"]["facts"]
        ))
        self.assertTrue(any(
            "<script>alert(1)</script>" in json.dumps(slide)
            for slide in result["deck"]["slides"]
        ))
        self.assertEqual(result["deck"]["profile"]["gaps"], [])

        shared = self.client.get(f"/api/share/{self.deck_id}")
        self.assertEqual(shared.status_code, 200)
        self.assertNotIn(self.token, shared.text)
        share_page = self.client.get(f"/share/{self.deck_id}")
        self.assertEqual(share_page.status_code, 200)
        self.assertNotIn("<script>alert(1)</script>", share_page.text)

    def test_story_studio_static_assets_are_served(self):
        page = self.client.get("/rp/static/index.html")
        app_js = self.client.get("/rp/static/app.js")
        css = self.client.get("/rp/static/studio.css")

        self.assertEqual(page.status_code, 200)
        self.assertIn("AI story editor", page.text)
        self.assertEqual(app_js.status_code, 200)
        self.assertIn("Add to my story", app_js.text)
        self.assertEqual(css.status_code, 200)
        self.assertIn(".broadcast-stage", css.text)

    def test_owner_token_file_is_private(self):
        token_file = os.path.join(self.temp_dir.name, self.deck_id, "owner.token")
        self.assertEqual(os.stat(token_file).st_mode & 0o777, 0o600)
        self.assertTrue(main.can_edit_deck(self.deck_id, self.token))
        self.assertFalse(main.can_edit_deck(self.deck_id, "wrong-token"))


if __name__ == "__main__":
    unittest.main()
