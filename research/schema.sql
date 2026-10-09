CREATE TABLE IF NOT EXISTS research_source_events (
    source text NOT NULL CHECK (source IN ('kudago', 'timepad')),
    external_id text NOT NULL,
    title text NOT NULL,
    city text,
    starts_at timestamptz,
    venue text,
    source_url text,
    artist_candidates jsonb NOT NULL,
    source_payload jsonb NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, external_id)
);
