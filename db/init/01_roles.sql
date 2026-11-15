-- Read-only role for smoke tests and future read-only dashboards (e.g. Grafana)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'prana_ro') THEN
        CREATE ROLE prana_ro WITH LOGIN PASSWORD 'prana_ro';
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO prana_ro', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO prana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO prana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO prana_ro;
