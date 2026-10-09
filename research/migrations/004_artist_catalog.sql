ALTER TABLE artists
    ADD COLUMN IF NOT EXISTS musicbrainz_id uuid UNIQUE;

CREATE TABLE IF NOT EXISTS artist_search_cache (
    query_key text PRIMARY KEY,
    candidates jsonb NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS artist_search_rate (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    next_request_at timestamptz NOT NULL DEFAULT '-infinity'
);

INSERT INTO artist_search_rate (singleton) VALUES (true) ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations (version) VALUES (4) ON CONFLICT DO NOTHING;
