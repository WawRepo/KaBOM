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

HOME-232 adds the browser UI on top of the same data: three Jinja2-rendered
screens (/, /sboms, /sboms/{id}) plus a freshness banner that is computed
here (_freshness_banner) and injected into every rendered page. The JSON API
routes above are untouched; the UI routes below call the same small query
helpers so there is exactly one place that knows the SQL, whether the caller
wants JSON or HTML.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kabom import db
from kabom.config import load_db_path, load_refresh_minutes, load_s3_config
from kabom.s3_client import S3UnavailableError

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=_PACKAGE_DIR / "templates")

# Freshness thresholds for the banner (HOME-232). Chosen against the default
# hourly refresh (KABOM_REFRESH_MINUTES): a normal refresh cycle should never
# by itself push a healthy dataset past GREEN.
#
#   GREEN  age < 24 hours
#   AMBER  24 hours <= age < 7 days
#   RED    age >= 7 days, OR age unknown, OR any sbom failed to read this run
_FRESHNESS_GREEN_MAX_SECONDS = 24 * 60 * 60
_FRESHNESS_AMBER_MAX_SECONDS = 7 * 24 * 60 * 60


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
app.mount("/static", StaticFiles(directory=_PACKAGE_DIR / "static"), name="static")


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
    return _search_rows(conn, q, version)


def _search_rows(conn: sqlite3.Connection, q: str, version: str | None = None) -> list[dict]:
    """Shared by GET /api/search and the "/" search page (HOME-232) — one
    place that knows the SQL, whether the caller wants JSON or HTML."""
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
    return _sboms_rows(conn)


def _sboms_rows(conn: sqlite3.Connection) -> list[dict]:
    """Shared by GET /api/sboms and the "/sboms" inventory page (HOME-232)."""
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
    row = _sbom_detail_row(conn, sbom_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No sbom with id {sbom_id}")
    return row


def _sbom_detail_row(conn: sqlite3.Connection, sbom_id: int) -> dict | None:
    """Shared by GET /api/sboms/{id} and the "/sboms/{id}" contents page
    (HOME-232). None means no sbom with that id."""
    row = conn.execute(
        "SELECT id, subject, kind, generated_at FROM sbom WHERE id = ?",
        (sbom_id,),
    ).fetchone()
    if row is None:
        return None

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
    return _status_dict(conn)


def _status_dict(conn: sqlite3.Connection) -> dict:
    """Shared by GET /api/status and every UI page's freshness banner
    (HOME-232) — the banner reads exactly what this endpoint reports, never
    a second, possibly-drifted computation of the same thing."""
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


# --- HOME-232: the UI — three screens and the freshness banner --------------
#
# Jinja2 + HTMX + Tailwind (standalone CLI), server-rendered, no JS bundler,
# no node_modules in the runtime image — see CLAUDE.md and the ticket. Every
# route below renders a full HTML page; there is no separate JSON-for-the-UI
# endpoint, so a browser with HTMX blocked or JavaScript disabled still gets
# a fully working page from a plain <form method="get"> submit.


def _age_seconds(generated_at: str | None) -> float | None:
    """Age, in seconds, of one `generated_at` ISO timestamp. None (unknown)
    for a missing timestamp — never treated as fresh, same rule as
    _oldest_sbom_age_seconds above."""
    if generated_at is None:
        return None
    return (datetime.now(UTC) - _as_utc(datetime.fromisoformat(generated_at))).total_seconds()


def _humanize_age(seconds: float | None) -> str:
    """A short, human phrase for an age in seconds, e.g. "20 minutes",
    "2 days". "unknown" when the age itself is unknown (see _age_seconds)."""
    if seconds is None:
        return "unknown"
    seconds = max(seconds, 0)
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        n = int(minutes)
        return f"{n} minute{'s' if n != 1 else ''}"
    hours = seconds / 3600
    if hours < 24:
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''}"
    days = seconds / 86400
    n = int(days)
    return f"{n} day{'s' if n != 1 else ''}"


def _freshness_banner(status: dict) -> dict:
    """Turn /api/status's raw numbers into what the banner shows.

    Uses the OLDEST sbom's age (status["age_seconds"]), never the newest or
    an average — see CLAUDE.md's "the age shown is always that of the oldest
    SBOM". Any failed read forces RED regardless of age: a file that could
    not be parsed this run is exactly the kind of thing that makes "we don't
    have that package" an unsafe answer, however fresh everything else is.

    level is one of "green", "amber", "red" — the template picks colour and
    the exact wording from it; kabom/templates/_search_results.html also
    reads it to draw the red border around search results when stale.
    """
    age = status["age_seconds"]
    seen = status["sboms_seen"]
    failed = status["sboms_failed"]
    read_ok = seen - failed

    if age is None or age >= _FRESHNESS_AMBER_MAX_SECONDS or failed > 0:
        level = "red"
    elif age >= _FRESHNESS_GREEN_MAX_SECONDS:
        level = "amber"
    else:
        level = "green"

    read_line = f"{read_ok} of {seen} read" if seen else "no SBOMs read yet"

    if level == "red":
        headline = f"STALE — oldest data is {_humanize_age(age)} old · {read_line}"
    else:
        headline = f"Updated {_humanize_age(age)} ago · {read_line}"

    return {"level": level, "headline": headline}


@app.get("/", response_class=HTMLResponse)
def index_page(
    request: Request, q: str | None = None, conn: sqlite3.Connection = Depends(get_db_connection)
) -> HTMLResponse:
    """The search screen: one big search box, focused on load, and nothing
    else. Results are grouped by package name — each row says which
    image/host it came from, its version, and how old that SBOM is.

    HTMX drives live, debounced results as you type (see
    kabom/templates/index.html); with HTMX unavailable this is a plain form
    that reloads "/" with ?q=... and gets the exact same rendering.
    """
    banner = _freshness_banner(_status_dict(conn))
    groups: list[dict] = []
    if q:
        rows = _search_rows(conn, q)
        by_name: dict[str, list[dict]] = {}
        for row in rows:
            by_name.setdefault(row["name"], []).append(
                {
                    "subject": row["subject"],
                    "kind": row["kind"],
                    "version": row["version"],
                    "age_human": _humanize_age(_age_seconds(row["generated_at"])),
                }
            )
        groups = [{"name": name, "rows": by_name[name]} for name in sorted(by_name)]

    return templates.TemplateResponse(
        request, "index.html", {"q": q, "groups": groups, "banner": banner}
    )


@app.get("/sboms", response_class=HTMLResponse)
def sboms_page(
    request: Request, conn: sqlite3.Connection = Depends(get_db_connection)
) -> HTMLResponse:
    """The inventory screen: every SBOM as a card, sorted OLDEST first so
    anything stuck is at the top rather than buried alphabetically.

    An unknown age (see _age_seconds) sorts as if it were infinitely old —
    an sbom with no timestamp at all is at least as concerning as one that
    is simply very old, never less.
    """
    banner = _freshness_banner(_status_dict(conn))
    rows = _sboms_rows(conn)
    sboms = [{**row, "age_human": _humanize_age(_age_seconds(row["generated_at"]))} for row in rows]
    sboms.sort(key=lambda s: _age_seconds(s["generated_at"]) or float("inf"), reverse=True)

    return templates.TemplateResponse(request, "sboms.html", {"sboms": sboms, "banner": banner})


@app.get("/sboms/{sbom_id}", response_class=HTMLResponse)
def sbom_detail_page(
    sbom_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_connection),
) -> HTMLResponse:
    """The contents screen: one SBOM's full component list, filterable in
    the browser (plain vanilla JS — see kabom/templates/sbom_detail.html;
    with JavaScript unavailable the full list is still there, just
    unfiltered, never a dead control)."""
    banner = _freshness_banner(_status_dict(conn))
    row = _sbom_detail_row(conn, sbom_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No sbom with id {sbom_id}")

    sbom = {**row, "age_human": _humanize_age(_age_seconds(row["generated_at"]))}
    return templates.TemplateResponse(request, "sbom_detail.html", {"sbom": sbom, "banner": banner})
