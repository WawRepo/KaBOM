-- KaBOM SQLite schema.
--
-- Three tables, on purpose — see HOME-230 and CLAUDE.md: 27 files, one user.
-- Plain sqlite3, no ORM, no migrations. If this schema changes, drop the
-- database file and re-ingest; the source of truth is S3 and a full rebuild
-- takes seconds.
--
-- This file must live on local-path storage, never nas-smb: SMB gives no
-- POSIX locking and SQLite corrupts on it (see CLAUDE.md's traps table).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sbom (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject      TEXT,
    kind         TEXT NOT NULL CHECK (kind IN ('image', 'host')),
    generated_at TEXT,
    source_key   TEXT NOT NULL,
    ingested_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sbom_id INTEGER NOT NULL REFERENCES sbom (id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    version TEXT,
    type    TEXT,
    purl    TEXT
);

-- The entire performance strategy: ~27 SBOMs of a few hundred components
-- each fit in RAM many times over, but a name lookup should still use an
-- index rather than a table scan.
CREATE INDEX IF NOT EXISTS idx_component_name ON component (name);

CREATE TABLE IF NOT EXISTS run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    sboms_seen   INTEGER NOT NULL DEFAULT 0,
    sboms_failed INTEGER NOT NULL DEFAULT 0,
    ok           INTEGER NOT NULL CHECK (ok IN (0, 1))
);
