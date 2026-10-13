SELECT format('CREATE ROLE gigradar_monitor LOGIN PASSWORD %L', :'monitor_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gigradar_monitor') \gexec
SELECT format('ALTER ROLE gigradar_monitor PASSWORD %L', :'monitor_password') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO gigradar_monitor', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO gigradar_monitor;
REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM gigradar_monitor;
ALTER DEFAULT PRIVILEGES FOR ROLE gigradar IN SCHEMA public
    REVOKE SELECT ON TABLES FROM gigradar_monitor;

CREATE OR REPLACE VIEW monitor_sync_state AS
    SELECT source, city, last_successful_at, event_count, rejected_count
    FROM research_sync_state;
CREATE OR REPLACE VIEW monitor_concerts AS
    SELECT id, city, starts_at, status FROM concerts;
CREATE OR REPLACE VIEW monitor_source_events AS
    SELECT source, external_id, concert_id, city, starts_at, classification,
           is_active, last_seen_at FROM research_source_events;
CREATE OR REPLACE VIEW monitor_concert_artists AS
    SELECT concert_id FROM concert_artists;
CREATE OR REPLACE VIEW monitor_artist_mentions AS
    SELECT source, external_id, confidence, reviewed FROM artist_mentions;
CREATE OR REPLACE VIEW monitor_duplicate_reviews AS
    SELECT 1 AS entry FROM duplicate_reviews;
CREATE OR REPLACE VIEW monitor_notifications AS
    SELECT kind, delivery_state, due_at, claimed_at, sent_at, attempts,
           failure_reason, last_error FROM notification_outbox;
CREATE OR REPLACE VIEW monitor_users AS
    SELECT created_at FROM users;
CREATE OR REPLACE VIEW monitor_user_activity AS
    SELECT activity_date FROM user_activity;
CREATE OR REPLACE VIEW monitor_product_events AS
    SELECT occurred_at, kind, outcome FROM product_events;
CREATE OR REPLACE VIEW monitor_subscriptions AS
    SELECT 1 AS entry FROM user_artist_subscriptions;
CREATE OR REPLACE VIEW monitor_favorites AS
    SELECT concert_id FROM user_favorites;

GRANT SELECT ON monitor_sync_state, monitor_concerts, monitor_source_events,
    monitor_concert_artists, monitor_artist_mentions, monitor_duplicate_reviews,
    monitor_notifications, monitor_users, monitor_user_activity,
    monitor_product_events, monitor_subscriptions, monitor_favorites
    TO gigradar_monitor;
