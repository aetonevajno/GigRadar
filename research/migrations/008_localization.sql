ALTER TABLE users
    ADD COLUMN locale text NOT NULL DEFAULT 'ru'
        CHECK (locale IN ('ru', 'en'));

INSERT INTO schema_migrations (version) VALUES (8) ON CONFLICT DO NOTHING;
