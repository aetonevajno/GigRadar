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

CREATE INDEX IF NOT EXISTS research_source_events_city_start_idx
    ON research_source_events (city, starts_at);

CREATE TABLE IF NOT EXISTS research_sync_state (
    source text NOT NULL CHECK (source = 'timepad'),
    city text NOT NULL,
    window_start date NOT NULL,
    window_end date NOT NULL,
    last_successful_at timestamptz NOT NULL,
    event_count integer NOT NULL,
    rejected_count integer NOT NULL,
    PRIMARY KEY (source, city)
);
