CREATE TABLE IF NOT EXISTS web_login_attempts (
    state_hash bytea PRIMARY KEY,
    code_verifier text NOT NULL,
    nonce text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS web_login_attempts_expires_idx
    ON web_login_attempts (expires_at);

CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash bytea PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS web_sessions_expires_idx
    ON web_sessions (expires_at);

INSERT INTO schema_migrations (version) VALUES (3) ON CONFLICT DO NOTHING;
