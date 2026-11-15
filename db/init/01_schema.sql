-- Multi-user extensible schema for Prana

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    ingest_token_hash TEXT NOT NULL UNIQUE,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    notification_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS raw_ingestions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_ingestions_user_source_time
    ON raw_ingestions (user_id, source, received_at DESC);

CREATE TABLE IF NOT EXISTS biomarkers (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    value DOUBLE PRECISION,
    value_min DOUBLE PRECISION,
    value_max DOUBLE PRECISION,
    unit TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, source, metric_name, recorded_at)
);

CREATE INDEX IF NOT EXISTS idx_biomarkers_user_metric_time
    ON biomarkers (user_id, metric_name, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_biomarkers_user_source_time
    ON biomarkers (user_id, source, recorded_at DESC);

CREATE TABLE IF NOT EXISTS workouts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    workout_type TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    duration_sec DOUBLE PRECISION,
    calories DOUBLE PRECISION,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, source, workout_type, started_at)
);

CREATE INDEX IF NOT EXISTS idx_workouts_user_time
    ON workouts (user_id, started_at DESC);
