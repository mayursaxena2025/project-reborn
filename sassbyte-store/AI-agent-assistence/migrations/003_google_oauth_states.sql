CREATE TABLE IF NOT EXISTS oauth_login_states (
    state_digest BYTEA PRIMARY KEY,
    nonce_digest BYTEA NOT NULL,
    pkce_challenge TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS oauth_login_states_expiry
    ON oauth_login_states (expires_at);
