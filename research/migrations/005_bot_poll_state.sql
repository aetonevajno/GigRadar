CREATE TABLE IF NOT EXISTS bot_poll_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    last_update_id bigint NOT NULL DEFAULT -1
);

INSERT INTO bot_poll_state (singleton) VALUES (true) ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations (version) VALUES (5) ON CONFLICT DO NOTHING;
