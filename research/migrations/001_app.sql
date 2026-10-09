CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS concerts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title text NOT NULL,
    city text NOT NULL,
    starts_at timestamptz NOT NULL,
    venue text,
    status text NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'postponed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS concerts_city_start_idx ON concerts (city, starts_at);

ALTER TABLE research_source_events
    ADD COLUMN IF NOT EXISTS concert_id bigint REFERENCES concerts(id),
    ADD COLUMN IF NOT EXISTS classification text NOT NULL DEFAULT 'unclear'
        CHECK (classification IN ('concert', 'not_concert', 'unclear')),
    ADD COLUMN IF NOT EXISTS classification_reason text NOT NULL DEFAULT 'not_classified',
    ADD COLUMN IF NOT EXISTS source_status text NOT NULL DEFAULT 'scheduled'
        CHECK (source_status IN ('scheduled', 'cancelled'));

CREATE INDEX IF NOT EXISTS source_events_concert_idx
    ON research_source_events (concert_id);

CREATE TABLE IF NOT EXISTS artists (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    name_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS artist_aliases (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    artist_id bigint NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    alias text NOT NULL,
    alias_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS artist_mentions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source text NOT NULL,
    external_id text NOT NULL,
    artist_id bigint REFERENCES artists(id),
    extracted_name text NOT NULL,
    field text NOT NULL,
    evidence text NOT NULL,
    confidence text NOT NULL CHECK (confidence IN ('high', 'review')),
    reviewed boolean NOT NULL DEFAULT false,
    UNIQUE (source, external_id, extracted_name, field, evidence),
    FOREIGN KEY (source, external_id)
        REFERENCES research_source_events (source, external_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS concert_artists (
    concert_id bigint NOT NULL REFERENCES concerts(id) ON DELETE CASCADE,
    artist_id bigint NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    PRIMARY KEY (concert_id, artist_id)
);

CREATE TABLE IF NOT EXISTS duplicate_reviews (
    first_source text NOT NULL,
    first_external_id text NOT NULL,
    second_source text NOT NULL,
    second_external_id text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (first_source, first_external_id, second_source, second_external_id)
);

CREATE TABLE IF NOT EXISTS concert_changes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concert_id bigint NOT NULL REFERENCES concerts(id) ON DELETE CASCADE,
    change_type text NOT NULL CHECK (change_type IN ('rescheduled', 'cancelled', 'restored')),
    old_value text,
    new_value text,
    source text NOT NULL,
    external_id text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_id bigint NOT NULL UNIQUE,
    display_name text NOT NULL,
    username text,
    city text,
    notifications_enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_artist_subscriptions (
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    artist_id bigint NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, artist_id)
);

CREATE TABLE IF NOT EXISTS user_favorites (
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    concert_id bigint NOT NULL REFERENCES concerts(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, concert_id)
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    concert_id bigint NOT NULL REFERENCES concerts(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('new_concert', 'reminder', 'rescheduled', 'cancelled')),
    change_id bigint REFERENCES concert_changes(id),
    dedupe_key text NOT NULL UNIQUE,
    due_at timestamptz NOT NULL,
    sent_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES (1) ON CONFLICT DO NOTHING;
