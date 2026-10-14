# Resume Story Builder (Sassbyte product)

Upload PDF/DOCX -> Claude drafts a source-grounded story -> candidate reviews and enriches it with a text AI coach -> a news-style presenter narrates the approved scenes -> PPTX/share export.

## Product architecture

- **Build mode:** the candidate reviews a structured profile and chats with the text-based story editor. The avatar is not a chat interface.
- **Story profile:** resume-derived facts, candidate-approved `user_chat` facts, and AI interpretations are separately labeled. Candidate additions are only saved after approval and are included in the regenerated deck.
- **Presenter mode:** a separate broadcast-style screen uses browser speech synthesis and scene changes to present the story.
- **Edit access:** upload returns a high-entropy edit token. The browser keeps it in session storage and sends it only to the interview and story-fact endpoints. The server stores the token separately from the public deck with owner-only file permissions.
- **Privacy:** original resume files and extracted source text are processed in memory. Generated profile facts and scenes are persisted under `~/persist/storyboard` for share links; anyone with a share link can view the presentation. Edit tokens and chat history are not included in public share responses.

## Run locally
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...
    uvicorn main:app --reload      # http://localhost:8000

## Deploy (Docker)
    docker build -t resume-presenter .
    docker run -d --restart unless-stopped -p 127.0.0.1:8000:8000 --env-file .env resume-presenter

## Add as a tab on sassybyte.site
Option A - subpath (recommended), nginx inside your existing server block:

    location /resume-presenter/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_read_timeout 120s;
        client_max_body_size 6m;
    }
Then add a nav link to `/resume-presenter/` in your portfolio site.

Option B - subdomain: point `resume.sassybyte.site` at the same proxy with `location / { proxy_pass ... }` and add the DNS A/CNAME record.

The frontend uses relative URLs, so both options work with no code changes. Use HTTPS (certbot).

## Notes
- Decks are cached in memory for 24h and persisted for share links. Edit credentials are separate from public deck JSON.
- Add rate limiting (e.g. nginx `limit_req`) - each upload costs one Claude call.
- Scanned-PDF OCR and server-side TTS/MP4 export are not included. Presenter narration uses browser speech synthesis.
