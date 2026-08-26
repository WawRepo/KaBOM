"""SQLite schema setup and transactional ingest.

Plain `sqlite3` and `schema.sql` — no ORM, no migrations, per CLAUDE.md and
HOME-230. If the schema changes, drop the database file and re-ingest; the
source of truth is S3 (kabom.ingest) and a full rebuild takes seconds.

This module writes the already-parsed output of kabom.ingest.ingest_all into
SQLite. It does not talk to S3 itself and does not expose an HTTP route or a
background timer — those are app-wiring concerns for HOME-231 (the search
API is what actually needs a place to trigger a refresh from). This module
only needs to guarantee one thing well: a failed or interrupted write must
never leave the database half-changed.

Never point the database file at `nas-smb` — SMB gives no POSIX locking and
SQLite corrupts on it. See CLAUDE.md's traps table; it goes on `local-path`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kabom.config import S3Config
from kabom.cyclonedx import ParsedSBOM
from kabom.ingest import ingest_all
from kabom.s3_client import S3UnavailableError

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection configured for explicit transactions.

    `isolation_level=None` puts the connection in autocommit mode so this
    module controls BEGIN/COMMIT/ROLLBACK itself instead of relying on
    sqlite3's implicit-transaction guessing. Foreign keys are off by default
    per-connection in SQLite, and schema.sql's `ON DELETE CASCADE` depends on
    them being on.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if it does not already exist. Safe to call every
    startup — schema.sql uses CREATE TABLE/INDEX IF NOT EXISTS throughout."""
    conn.executescript(_SCHEMA_PATH.read_text())


@dataclass(frozen=True)
class RunSummary:
    """What one ingest attempt did — mirrors one row written to `run`."""

    ok: bool
    sboms_seen: int
    sboms_failed: int


def infer_kind(source_key: str) -> str:
    """Guess whether an SBOM describes a container image or a host.

    HOME-229's ParsedSBOM carries no explicit kind field, but schema.sql's
    `sbom.kind` column requires one of 'image' or 'host'. The nightly job
    (HOME-224) lays files out under a leading "hosts/" or "images/" key
    prefix — matching the sample fixtures used in tests/test_ingest.py.
    Anything that matches neither defaults to 'host' rather than raising: a
    mislabeled row is still fully searchable, which matters more here than
    getting kind exactly right for a key shape nobody has seen yet.
    """
    first_segment = source_key.split("/", 1)[0].lower()
    return "image" if "image" in first_segment else "host"


def run_ingest(conn: sqlite3.Connection, config: S3Config) -> RunSummary:
    """Run one ingest pass and replace the database contents transactionally.

    Calls kabom.ingest.ingest_all(config) to read and parse every SBOM in
    the bucket, then replaces the sbom/component tables in a single
    transaction. Every attempt — success or failure — writes one row to
    `run` so the UI can say how fresh the data is and whether the last
    refresh actually worked.

    On any failure, the previous sbom/component contents are left completely
    intact:

    - If ingest_all cannot even reach S3 (S3UnavailableError), the database
      is never touched.
    - If something raises partway through the database write (a bad row, a
      disk error, anything), the transaction is rolled back before the
      exception propagates, so callers see the same data as before this
      call.

    A half-replaced database would let the search feature answer "we don't
    have that package" for a package that was simply never re-loaded — see
    CLAUDE.md's "must never show stale data as though it were current."

    Callers must call init_db(conn) at least once before this.
    """
    started_at = _now_iso()

    try:
        result = ingest_all(config)
    except S3UnavailableError:
        logger.exception("Ingest failed: could not read SBOMs from S3")
        _record_run(conn, started_at, ok=False, sboms_seen=0, sboms_failed=0)
        raise

    sboms_seen = len(result.sboms) + len(result.errors)
    sboms_failed = len(result.errors)

    conn.execute("BEGIN")
    try:
        _replace_all(conn, result.sboms)
    except Exception:
        conn.execute("ROLLBACK")
        logger.exception("Ingest failed: database write did not complete, previous data kept")
        _record_run(conn, started_at, ok=False, sboms_seen=sboms_seen, sboms_failed=sboms_failed)
        raise
    else:
        conn.execute("COMMIT")

    _record_run(conn, started_at, ok=True, sboms_seen=sboms_seen, sboms_failed=sboms_failed)
    return RunSummary(ok=True, sboms_seen=sboms_seen, sboms_failed=sboms_failed)


def _replace_all(conn: sqlite3.Connection, sboms: list[ParsedSBOM]) -> None:
    """Delete every existing sbom/component row and insert the new set.

    Must run inside a caller-managed transaction (see run_ingest) — it issues
    no BEGIN/COMMIT/ROLLBACK of its own so a failure partway through leaves
    the decision to the caller.

    ingest_all always reads the entire bucket, so a full replace here is
    equivalent to "replace rows per subject, old rows removed" and simpler
    to reason about — see CLAUDE.md's "take simple". `ON DELETE CASCADE` on
    component.sbom_id removes the matching components for free.
    """
    conn.execute("DELETE FROM sbom")
    ingested_at = _now_iso()
    for sbom in sboms:
        cursor = conn.execute(
            "INSERT INTO sbom (subject, kind, generated_at, source_key, ingested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sbom.subject_name,
                infer_kind(sbom.source_key),
                sbom.generated_at.isoformat() if sbom.generated_at else None,
                sbom.source_key,
                ingested_at,
            ),
        )
        sbom_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO component (sbom_id, name, version, type, purl) VALUES (?, ?, ?, ?, ?)",
            [(sbom_id, c.name, c.version, c.type, c.purl) for c in sbom.components],
        )


def _record_run(
    conn: sqlite3.Connection, started_at: str, *, ok: bool, sboms_seen: int, sboms_failed: int
) -> None:
    conn.execute(
        "INSERT INTO run (started_at, finished_at, sboms_seen, sboms_failed, ok) "
        "VALUES (?, ?, ?, ?, ?)",
        (started_at, _now_iso(), sboms_seen, sboms_failed, int(ok)),
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
