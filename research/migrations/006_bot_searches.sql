CREATE TABLE IF NOT EXISTS bot_searches (
    token text PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('concert', 'artist')),
    query text NOT NULL,
    city text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS bot_searches_created_idx ON bot_searches (created_at);

INSERT INTO schema_migrations (version) VALUES (6) ON CONFLICT DO NOTHING;
