from fastapi.testclient import TestClient

import main


def test_persisted_deck_reloads_after_cache_clear():
    client = TestClient(main.app)
    did = "replay-test-123"
    deck = {
        "name": "Replay Test",
        "gender": "N",
        "slides": [
            {"t": "title", "h": "Welcome", "s": "Sample deck", "n": "Hello world"},
            {"t": "list", "h": "Points", "items": [["One", "Detail one"]], "n": "Next slide"},
        ],
    }
    main.persist_storyboard(did, deck)
    main.DECKS.clear()

    resp = client.get(f"/api/deck/{did}")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["name"] == "Replay Test"
    assert payload["slides"][0]["h"] == "Welcome"


def test_share_page_includes_narration_controls():
    page = main.render_share_page("share-test", {
        "name": "Narration Test",
        "slides": [{"t": "title", "h": "Welcome", "n": "Hello {name}"}],
    })

    assert 'id="play">Play narration</button>' in page
    assert 'id="stopAudio"' in page
    assert "SpeechSynthesisUtterance" in page
