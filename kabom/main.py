"""FastAPI application entrypoint.

Wires kabom.db's run_ingest into the app (HOME-231):

- One ingest pass runs at startup, so the database is never empty-because-
  nobody-asked on a fresh boot.
- POST /admin/refresh triggers a pass on demand.
- A single asyncio background task re-ingests every KABOM_REFRESH_MINUTES
  (default 60) — a plain timer, not a job queue, per CLAUDE.md's "take
  simple": this is a single-process homelab app.

Every ingest failure is logged and recorded in the `run` table, and the app
keeps serving whatever it already has — see CLAUDE.md's "must never show
stale data as though it were current" (which is a call to always disclose
the age, not a license to hide old data behind a crash).

This module also implements the search API — the one real feature — and
GET /healthz, unchanged from HOME-228: no auth, no DB dependency, always ok
if the process is up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException

from kabom import db
from kabom.config import load_db_path, load_refresh_minutes, load_s3_config
from kabom.s3_client import S3UnavailableError

logger = logging.getLogger(__name__)


def _run_ingest_once() -> db.RunSummary | None:
    """Open a connection, run one ingest pass, close it.

    Never raises: ingest failures (S3 unreachable, a bad write) are logged
    and already recorded as a failed `run` row by db.run_ingest itself. The
    previous sbom/component contents are left intact either way, so the app
    keeps answering search with its last-known-good data, honestly labelled
    by /api/status's age_seconds — never a crash, never silent staleness.
    """
    conn = db.get_connection(load_db_path())
    try:
        db.init_db(conn)
        try:
            return db.run_ingest(conn, load_s3_config())
        except Exception:
            logger.exception("Ingest failed; keeping previous data")
            return None
    finally:
        conn.close()


async def _refresh_loop(minutes: int) -> None:
    """Background timer: re-ingest every `minutes` minutes until cancelled.

    Sleeps first, since _run_ingest_once already ran once at startup — no
    need to do the same work twice back to back.
    """
    while True:
        await asyncio.sleep(minutes * 60)
        await asyncio.to_thread(_run_ingest_once)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.get_connection(load_db_path())
    db.init_db(conn)
    conn.close()

    await asyncio.to_thread(_run_ingest_once)

    task = asyncio.create_task(_refresh_loop(load_refresh_minutes()))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="KaBOM", lifespan=lifespan)


def get_db_connection() -> Iterator[sqlite3.Connection]:
    """Per-request SQLite connection.

    A fresh connection per request, opened and closed on the spot, rather
    than one long-lived connection shared across threads — sqlite3
    connections are not meant to be handed between threads, and FastAPI runs
    sync path operations in a thread pool. Opening a file-backed SQLite
    connection is cheap; this is the simple option, not a performance
    compromise, at 27 SBOMs.

    Tests override this dependency to point at a temporary database instead
    of the configured KABOM_DB_PATH.
    """
    conn = db.get_connection(load_db_path())
    try:
        yield conn
    finally:
        conn.close()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness check. Always returns ok if the process is up."""
    return {"status": "ok"}


@app.post("/admin/refresh")
def trigger_refresh(conn: sqlite3.Connection = Depends(get_db_connection)) -> dict:
    """Trigger one ingest pass on demand, synchronously, and report what it did."""
    db.init_db(conn)
    try:
        summary = db.run_ingest(conn, load_s3_config())
    except S3UnavailableError as exc:
        return {"ok": False, "sboms_seen": 0, "sboms_failed": 0, "error": str(exc)}
    return {
        "ok": summary.ok,
        "sboms_seen": summary.sboms_seen,
        "sboms_failed": summary.sboms_failed,
    }


@app.get("/api/search")
def search(
    q: str,
    version: str | None = None,
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> list[dict]:
    """Search components by name (case-insensitive substring) across every
    ingested SBOM. `version`, when given, must match exactly.

    No pagination, no fuzzy matching, no ranking — a plain indexed LIKE is
    correct and fast at 27 SBOMs (see HOME-231). Every result row carries its
    own `generated_at` so a caller never has to make a second request to
    know how much to trust it. Nothing found is a normal, successful answer:
    an empty list with HTTP 200, never a 404.
    """
    query = (
        "SELECT s.subject, s.kind, c.name, c.version, c.purl, s.generated_at "
        "FROM component c JOIN sbom s ON s.id = c.sbom_id "
        "WHERE c.name LIKE ?"
    )
    params: list[str] = [f"%{q}%"]
    if version is not None:
        query += " AND c.version = ?"
        params.append(version)

    rows = conn.execute(query, params).fetchall()
    return [
        {
            "subject": subject,
            "kind": kind,
            "name": name,
            "version": comp_version,
            "purl": purl,
            "generated_at": generated_at,
        }
        for subject, kind, name, comp_version, purl, generated_at in rows
    ]


@app.get("/api/sboms")
def list_sboms(conn: sqlite3.Connection = Depends(get_db_connection)) -> list[dict]:
    """Every ingested SBOM, with a component count, newest ingest first-ish
    (ordered by id — insertion order from the last ingest pass)."""
    rows = conn.execute(
        "SELECT s.id, s.subject, s.kind, s.generated_at, COUNT(c.id) "
        "FROM sbom s LEFT JOIN component c ON c.sbom_id = s.id "
        "GROUP BY s.id "
        "ORDER BY s.id"
    ).fetchall()
    return [
        {
            "id": sbom_id,
            "subject": subject,
            "kind": kind,
            "generated_at": generated_at,
            "component_count": component_count,
        }
        for sbom_id, subject, kind, generated_at, component_count in rows
    ]


@app.get("/api/sboms/{sbom_id}")
def get_sbom(sbom_id: int, conn: sqlite3.Connection = Depends(get_db_connection)) -> dict:
    """One SBOM with its full component list. 404 if the id does not exist."""
    row = conn.execute(
        "SELECT id, subject, kind, generated_at FROM sbom WHERE id = ?",
        (sbom_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No sbom with id {sbom_id}")

    found_id, subject, kind, generated_at = row
    component_rows = conn.execute(
        "SELECT name, version, type, purl FROM component WHERE sbom_id = ? ORDER BY id",
        (sbom_id,),
    ).fetchall()
    return {
        "id": found_id,
        "subject": subject,
        "kind": kind,
        "generated_at": generated_at,
        "components": [
            {"name": name, "version": version, "type": comp_type, "purl": purl}
            for name, version, comp_type, purl in component_rows
        ],
    }


@app.get("/api/status")
def status(conn: sqlite3.Connection = Depends(get_db_connection)) -> dict:
    """The latest ingest run, plus the age of the OLDEST sbom.

    Deliberately the oldest, not the newest and not the average: one SBOM
    stuck at 40 days while the other 26 refresh nightly is exactly the case
    that matters, and the other two statistics would hide it (see CLAUDE.md
    and HOME-231).
    """
    run_row = conn.execute(
        "SELECT finished_at, sboms_seen, sboms_failed, ok FROM run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if run_row is None:
        finished_at, sboms_seen, sboms_failed, ok = None, 0, 0, False
    else:
        finished_at, sboms_seen, sboms_failed, ok_int = run_row
        ok = bool(ok_int)

    return {
        "finished_at": finished_at,
        "sboms_seen": sboms_seen,
        "sboms_failed": sboms_failed,
        "ok": ok,
        "age_seconds": _oldest_sbom_age_seconds(conn),
    }


def _oldest_sbom_age_seconds(conn: sqlite3.Connection) -> float | None:
    """Age, in seconds, of the OLDEST sbom's generated_at.

    None means "unknown", which callers must treat as stale, never as
    fresh — either there are no sboms yet, or at least one has no
    generated_at at all (a missing timestamp is not evidence of freshness;
    see kabom/cyclonedx.py's ParsedSBOM docstring).
    """
    rows = conn.execute("SELECT generated_at FROM sbom").fetchall()
    if not rows:
        return None

    raw_timestamps = [row[0] for row in rows]
    if any(value is None for value in raw_timestamps):
        return None

    timestamps = [_as_utc(datetime.fromisoformat(value)) for value in raw_timestamps]
    oldest = min(timestamps)
    return (datetime.now(UTC) - oldest).total_seconds()


def _as_utc(value: datetime) -> datetime:
    """Normalize to an aware, UTC datetime so ages can be compared/subtracted
    regardless of what offset the source SBOM's timestamp used."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
