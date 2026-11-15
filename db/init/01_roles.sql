-- Read-only role for smoke tests and future read-only dashboards (e.g. Grafana)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'prana_ro') THEN
        CREATE ROLE prana_ro WITH LOGIN PASSWORD 'prana_ro';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE prana TO prana_ro;
GRANT USAGE ON SCHEMA public TO prana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO prana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO prana_ro;
