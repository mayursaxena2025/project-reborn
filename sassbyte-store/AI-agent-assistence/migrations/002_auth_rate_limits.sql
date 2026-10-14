CREATE TABLE IF NOT EXISTS auth_login_attempts (
    email_hash BYTEA PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until TIMESTAMPTZ
);
