-- FABRIK-9 PostgreSQL schema (reference DDL).
-- The app creates these automatically via SQLAlchemy on startup;
-- this file documents the production shape with JSONB and indexes.

CREATE TABLE simulation_runs (
    id          VARCHAR(32) PRIMARY KEY,
    seed        INTEGER NOT NULL,
    label       VARCHAR(120) DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE snapshots (
    id          VARCHAR(32) PRIMARY KEY,
    run_id      VARCHAR(32) NOT NULL REFERENCES simulation_runs(id),
    tick        INTEGER NOT NULL,
    label       VARCHAR(120) DEFAULT '',
    state       JSONB NOT NULL,          -- full serialized world
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_snapshots_run_tick ON snapshots (run_id, tick);

CREATE TABLE telemetry_samples (
    id            SERIAL PRIMARY KEY,
    run_id        VARCHAR(32) NOT NULL REFERENCES simulation_runs(id),
    tick          INTEGER NOT NULL,
    gears_stock   INTEGER,
    gears_crafted INTEGER,
    ore_delivered INTEGER,
    faults_active INTEGER,
    faults_fixed  INTEGER,
    mttr_s        DOUBLE PRECISION,
    agents        JSONB,                 -- per-agent position/state/cargo
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_telemetry_run_tick ON telemetry_samples (run_id, tick);

CREATE TABLE event_logs (
    id       SERIAL PRIMARY KEY,
    run_id   VARCHAR(32) NOT NULL REFERENCES simulation_runs(id),
    tick     INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    level    VARCHAR(16) DEFAULT '',
    message  TEXT NOT NULL
);
CREATE INDEX ix_events_run_tick ON event_logs (run_id, tick);
