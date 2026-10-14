# OpenLead

An open-source lead intelligence app for lead intake, qualification, CRM sync, SMS/voice workflow hooks, and appointment scheduling. The HTTP app now uses authenticated, private PostgreSQL workspaces; the production database and service have not yet been migrated from the legacy SQLite deployment.

## Productization foundation

The schema is in the numbered files under `migrations/`. It provides private workspaces, users, OAuth identities, sessions, email-verification/password-reset tokens, tenant-owned CRM records, encrypted integration-secret storage, login throttling, one-time Google OIDC state, and a durable integration-delivery outbox. The latest migration removes obsolete local-model settings from existing workspaces.

`auth.py` provides scrypt password hashing, verified-account password checks, private-workspace provisioning, one-time email token handling, hashed session/CSRF token primitives, SMTP delivery, persisted login throttling, and Google identity provisioning. `app.py` connects these flows to PostgreSQL-backed HTTP routes. Email/password registration requires email verification; sign-in creates secure, HTTP-only sessions, and all workspace data routes derive their workspace from that authenticated session. Google sign-in uses authorization-code OIDC, signed ID-token verification against Google's keys, one-time state and nonce, and PKCE. An existing email/password account is deliberately never auto-linked to Google by email; the user must use its established sign-in method.

`database.py` provides a bounded Psycopg pool, explicit versioned SQL migrations, workspace-scoped lead/appointment/activity/settings operations, encrypted per-workspace integration settings, and a durable delivery enqueue operation. Configure the pool using `OPENLEAD_DATABASE_URL`, `OPENLEAD_DB_POOL_MIN`, and `OPENLEAD_DB_POOL_MAX`. Configure `OPENLEAD_ENCRYPTION_KEY` separately; keep all secrets outside the source tree. The HTTP app fails closed with service-unavailable responses if PostgreSQL is not configured or unavailable; it does not fall back to SQLite.

`migrate_sqlite_to_postgres.py` imports the existing SQLite leads, appointments, activities, and settings into one designated owner workspace. It refuses to merge into a non-empty target, is guarded against a second import, requests a new owner password interactively, and encrypts CRM/webhook secrets using a Fernet key supplied out-of-band. Back up the source SQLite file and take an encrypted PostgreSQL backup before a planned migration. Do not run this against production until the PostgreSQL schema, service credentials, and restore procedure have been tested in staging.

Install the dependencies in an isolated environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Set `OPENLEAD_DATABASE_URL` to a PostgreSQL connection URL and `OPENLEAD_ENCRYPTION_KEY` to a Fernet key stored outside the database and source tree. Generate a key using `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` and store it in a secret manager. Account registration and password recovery also require `SMTP_HOST`, `SMTP_PORT` (optional; defaults to 587), `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, and an HTTPS `OPENLEAD_PUBLIC_URL`.

To enable Google sign-in, configure an OAuth 2.0 web client in Google Cloud, add the exact HTTPS callback URL as an authorized redirect URI, and provide `OPENLEAD_GOOGLE_CLIENT_ID`, `OPENLEAD_GOOGLE_CLIENT_SECRET`, and `OPENLEAD_GOOGLE_REDIRECT_URI`. The callback URI must share the host of `OPENLEAD_PUBLIC_URL` and must match Google's configured redirect URI exactly. The callback path is `/api/auth/google/callback` relative to the app's public mount path. Keep the client secret outside source control. Until the database and OAuth settings are configured, Google sign-in remains unavailable; email/password signup independently requires the email and encryption settings above.

To start the app locally after PostgreSQL and secrets are configured:

```sh
python3 app.py
```

Run backend tests from this directory with:

```sh
python3 -m unittest -v
```

To import the existing SQLite data into a designated owner workspace, first create a PostgreSQL backup and test the restore procedure. Then run:

```sh
python migrate_sqlite_to_postgres.py \
  --sqlite-path /path/to/crm-agent.sqlite3 \
  --owner-email owner@example.com \
  --owner-name "OpenLead Owner"
```

The migration marks this interactively provisioned owner email as verified. Never put the password, database URL, or encryption key in shell history, logs, source control, or this command line.

Requirements: Python 3.11 or newer. The listener defaults to `127.0.0.1:8000`; set `HOST` and `PORT` to change it. Do not expose an instance publicly until it is served over HTTPS and configured with production-grade database, SMTP, and secret management.

## AI

OpenLead supports public API access to **OpenAI**, **Anthropic**, and **Google Gemini**. In Connections, select a provider, enter a model name and API key. Obtain credentials directly from the provider; OpenLead does not provide or bundle API access. Keys are encrypted per workspace using `OPENLEAD_ENCRYPTION_KEY`, bound to their provider, and never returned to the browser after saving. Provider usage and billing are governed by the provider's account and terms.

Lead qualification and assistant chats send the necessary lead context to the configured provider. Do not enable a provider unless the workspace owner accepts that provider's data-handling and retention policies. If no public LLM is configured or qualification encounters a provider error, qualification falls back to the transparent local rules. The chat uses the selected provider and reports a provider failure rather than pretending to answer. The assistant receives a bounded CRM snapshot and does not execute CRM actions.

## Integrations

- **CRM:** Configure a JSON `POST` lead-create endpoint and optional bearer token. New leads are sent to this endpoint. Every CRM has a different schema and auth model; use the CRM's API directly when compatible or bridge it with a self-hosted tool such as n8n. The UI and backend expose one adapter contract; CRM-specific field mapping is not universal.
- **SMS:** Configure an SMS webhook that accepts `{event, channel, lead, message}`. The app requires recorded lead consent before dispatch. Connect a self-hosted provider/bridge such as an n8n workflow and an SMS gateway you control. This app does not include a carrier or send a text by itself.
- **Voice:** Configure a voice webhook. Explicit operator actions can dispatch call requests only where call consent is recorded. A separate voice stack (for example, Asterisk plus a voice agent) must handle telephony and return webhooks for call outcomes.
- **Appointments:** Scheduling is recorded locally and can emit an `appointment.created` event to a calendar or automation webhook.

Webhook payloads use JSON. Configured CRM, SMS, voice, and calendar endpoints must use HTTPS on port 443 and resolve exclusively to globally routable addresses. OpenLead pins each request to its validated DNS address, rejects private/local/link-local/reserved targets and redirects, and repeats the checks when settings are saved and when the webhook is called. Use a public HTTPS endpoint for a self-hosted bridge; internal/private bridge addresses are intentionally blocked. Test integrations with a non-production endpoint before connecting live customer data. Workspace APIs require a signed-in, email-verified account; mutations also require same-site CSRF protection. A public inbound intake API is not currently included.

## Current MVP boundary

This is an early multi-user foundation, not yet a production-ready SaaS. Production PostgreSQL provisioning/migration, Google and email credential configuration, durable webhook delivery workers, operational monitoring, and load/restore validation remain before a public multi-user deployment. Provider-specific CRM adapters, team workspaces, inbound telephony, carrier delivery, and calendar availability checks are also not included. Configure the reverse proxy to avoid logging query strings on the Google callback endpoint; authorization codes should not be retained in access logs.