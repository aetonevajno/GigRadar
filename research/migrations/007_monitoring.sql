ALTER TABLE notification_outbox
    ADD COLUMN IF NOT EXISTS failure_reason text
        CHECK (failure_reason IN ('ineligible', 'retry_limit', 'telegram'));

CREATE TABLE IF NOT EXISTS product_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    user_id bigint REFERENCES users(id) ON DELETE SET NULL,
    kind text NOT NULL CHECK (kind IN ('concert_search', 'artist_search',
        'subscription_add', 'subscription_remove', 'favorite_add', 'favorite_remove')),
    outcome text NOT NULL CHECK (outcome IN ('with_results', 'without_results', 'changed'))
);
CREATE INDEX IF NOT EXISTS product_events_time_idx ON product_events (occurred_at);
CREATE INDEX IF NOT EXISTS product_events_user_time_idx ON product_events (user_id, occurred_at);

CREATE TABLE IF NOT EXISTS user_activity (
    activity_date date NOT NULL,
    user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (activity_date, user_id)
);

INSERT INTO schema_migrations (version) VALUES (7) ON CONFLICT DO NOTHING;
