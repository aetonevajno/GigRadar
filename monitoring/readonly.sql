SELECT format('CREATE ROLE gigradar_monitor LOGIN PASSWORD %L', :'monitor_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gigradar_monitor') \gexec
SELECT format('ALTER ROLE gigradar_monitor PASSWORD %L', :'monitor_password') \gexec
GRANT CONNECT ON DATABASE gigradar TO gigradar_monitor;
GRANT USAGE ON SCHEMA public TO gigradar_monitor;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO gigradar_monitor;
ALTER DEFAULT PRIVILEGES FOR ROLE gigradar IN SCHEMA public
    GRANT SELECT ON TABLES TO gigradar_monitor;
