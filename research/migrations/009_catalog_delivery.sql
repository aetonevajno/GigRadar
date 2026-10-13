ALTER TABLE research_source_events
    ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS missing_count integer NOT NULL DEFAULT 0;

UPDATE research_source_events SET is_active = false WHERE source = 'kudago';
DELETE FROM concert_artists;
INSERT INTO concert_artists (concert_id, artist_id)
    SELECT DISTINCT s.concert_id, m.artist_id
    FROM research_source_events s
    JOIN artist_mentions m ON (m.source, m.external_id) = (s.source, s.external_id)
    WHERE s.classification = 'concert' AND s.is_active
      AND s.concert_id IS NOT NULL AND m.artist_id IS NOT NULL
    ON CONFLICT DO NOTHING;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS bot_chat_available boolean NOT NULL DEFAULT false;
ALTER TABLE users ALTER COLUMN notifications_enabled SET DEFAULT false;
UPDATE users SET bot_chat_available = true
WHERE EXISTS (SELECT 1 FROM bot_searches WHERE bot_searches.user_id = users.id);
UPDATE users SET city = NULL
WHERE city IS NOT NULL AND city NOT IN ('Москва', 'Санкт-Петербург');

ALTER TABLE notification_outbox DROP CONSTRAINT notification_outbox_kind_check;
ALTER TABLE notification_outbox ADD CONSTRAINT notification_outbox_kind_check
    CHECK (kind IN ('new_concert', 'reminder', 'rescheduled', 'cancelled', 'restored'));

INSERT INTO schema_migrations (version) VALUES (9) ON CONFLICT DO NOTHING;
