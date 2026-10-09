ALTER TABLE notification_outbox
    ADD COLUMN IF NOT EXISTS delivery_state text NOT NULL DEFAULT 'queued'
        CHECK (delivery_state IN ('queued', 'sending', 'sent', 'failed', 'uncertain')),
    ADD COLUMN IF NOT EXISTS claimed_at timestamptz,
    ADD COLUMN IF NOT EXISTS telegram_message_id bigint;

CREATE INDEX IF NOT EXISTS notification_ready_idx
    ON notification_outbox (due_at, id) WHERE delivery_state = 'queued';

INSERT INTO schema_migrations (version) VALUES (2) ON CONFLICT DO NOTHING;
