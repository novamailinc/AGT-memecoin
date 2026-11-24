-- 001_agt_meta.sql — AGT deployment metadata table.
--
-- Records which migrations have been applied (idempotency) and which
-- deployment created this database. Applied by deploy/agt/db_init.py
-- before the base schema; the base schema itself is applied from
-- storage/schemas/schema.sql and is NOT duplicated here.

CREATE TABLE IF NOT EXISTS agt_meta (
    kind       TEXT NOT NULL,   -- 'migration' | 'deployment'
    name       TEXT NOT NULL,   -- migration filename, or 'created'
    applied_ts TEXT NOT NULL,   -- UTC ISO-8601
    value      TEXT,            -- free-form detail (e.g. repo + branch)
    PRIMARY KEY (kind, name)
);
