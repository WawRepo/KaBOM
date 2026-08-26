"""FastAPI application entrypoint.

Wires kabom.db's run_ingest into the app (HOME-231):

- One ingest pass runs at startup, so the database is never empty-because-
  nobody-asked on a fresh boot.
- POST /admin/refresh triggers a pass on demand.
- A single asyncio background task re-ingests every KABOM_REFRESH_MINUTES
  (default 60) — a plain timer, not a job queue: this is a single-process
  app.

Every ingest failure is logged and recorded in the `run` table, and the app
keeps serving whatever it already has — the rule that stale data must never
look current is a call to always disclose
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
import os
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from kabom import auth, db
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

    Never raises — and that has to include opening the database, not just
    the ingest itself. `_refresh_loop` calls this forever; an exception
    escaping here kills that asyncio task for the life of the process, so
    refreshes stop permanently while /healthz stays green and nothing
    restarts the pod. Only the age banner would ever reveal it.

    Ingest failures (S3 unreachable, a bad write) are logged and already
    recorded as a failed `run` row by db.run_ingest itself. The previous
    sbom/component contents are left intact either way, so the app keeps
    answering search with its last-known-good data, honestly labelled by
    /api/status's age_seconds — never a crash, never silent staleness.
    """
    conn = None
    try:
        conn = db.get_connection(load_db_path())
        db.init_db(conn)
        return db.run_ingest(conn, load_s3_config())
    except Exception:
        logger.exception("Ingest failed; keeping previous data")
        return None
    finally:
        if conn is not None:
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
    # Validated first, before anything touches the database or S3: a
    # misconfigured auth mode (HOME-233) must stop the app from ever serving
    # a request, not just fail the first time someone happens to hit a
    # protected route. For KABOM_AUTH=google this re-checks what the
    # SessionMiddleware wiring below already validated at import time;
    # that's cheap and keeps this one function as the single "can this app
    # actually start" check regardless of mode.
    auth.validate_startup_config()

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

# --- auth --------------------------------------------------------------------
#
# Both modes sign a session cookie now: google mode after the OAuth
# callback, basic mode after the login form. Read KABOM_AUTH directly from
# the environment here, at import time, rather than through
# auth.load_auth_mode(): that function raises on anything but "basic" or
# "google", and importing this module must stay safe for both modes AND for
# every test in this repo that imports kabom.main without setting KABOM_AUTH
# at all (they override the auth dependency instead).
#
# This is deliberately the "refuse to start" moment for a missing
# KABOM_SESSION_SECRET: raising here stops `uvicorn kabom.main:app` (and
# `import kabom.main`) dead, before a single request can be served — not a
# generated secret, not a per-request 500 discovered later.
if os.environ.get("KABOM_AUTH") in ("basic", "google"):
    # `https_only` marks the cookie Secure, so a browser will not send it
    # back over plain HTTP. That is what production wants and what
    # docker-compose on http://localhost cannot live with — without the
    # opt-out, logging in there would appear to succeed and then silently
    # bounce straight back to the login page. Secure by default, explicit
    # to turn off, and never turned off anywhere but local dev.
    _insecure_cookies = os.environ.get("KABOM_INSECURE_COOKIES") == "1"
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.load_session_secret(),
        same_site="lax",
        https_only=not _insecure_cookies,
    )

# Every route below except GET /healthz is registered on this router, whose
# one dependency is the whole auth gate (kabom.auth.require_auth) — wired
# once here rather than repeated on every handler. /healthz stays directly
# on `app`, unauthenticated, for the Ingress health check.
protected = APIRouter(dependencies=[Depends(auth.require_auth)])


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


@protected.post("/admin/refresh")
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


@protected.get("/api/search")
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


@protected.get("/api/sboms")
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


@protected.get("/api/sboms/{sbom_id}")
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


@protected.get("/api/status")
def status(conn: sqlite3.Connection = Depends(get_db_connection)) -> dict:
    """The latest ingest run, plus the age of the OLDEST sbom.

    Deliberately the oldest, not the newest and not the average: one SBOM
    stuck at 40 days while the other 26 refresh nightly is exactly the case
    that matters, and the other two statistics would hide it.
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
# no node_modules in the runtime image. Every
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


def _age_label(seconds: float | None) -> str:
    """A whole phrase for an sbom's age, not a duration to wrap in "… ago".

    _humanize_age returns "unknown" and "just now", neither of which reads
    as a duration — "scanned just now ago" and, worse, "scanned unknown
    ago", where an unknown age is exactly the case that should read as
    alarming rather than as a typo.
    """
    if seconds is None:
        return "age unknown — treat as stale"
    if seconds < 60:
        return "scanned just now"
    return f"scanned {_humanize_age(seconds)} ago"


def _freshness_banner(status: dict) -> dict:
    """Turn /api/status's raw numbers into what the banner shows.

    Uses the OLDEST sbom's age (status["age_seconds"]), never the newest or
    an average: the age shown is always that of the oldest SBOM. Any
    failed read forces RED regardless of age: a file that could
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


@protected.get("/", response_class=HTMLResponse)
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
        request,
        "index.html",
        {"q": q, "groups": groups, "banner": banner, "active_nav": "/"},
    )


@protected.get("/sboms", response_class=HTMLResponse)
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

    return templates.TemplateResponse(
        request, "sboms.html", {"sboms": sboms, "banner": banner, "active_nav": "/sboms"}
    )


@protected.get("/sboms/{sbom_id}", response_class=HTMLResponse)
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

    age_seconds = _age_seconds(row["generated_at"])
    sbom = {
        **row,
        "age_human": _humanize_age(age_seconds),
        "age_label": _age_label(age_seconds),
    }
    return templates.TemplateResponse(
        request, "sbom_detail.html", {"sbom": sbom, "banner": banner, "active_nav": "/sboms"}
    )


app.include_router(protected)


# --- the login page ----------------------------------------------------------
#
# Registered on `app`, not `protected` — a login page you must already be
# logged in to reach is not a login page. In basic mode this is what
# kabom.auth redirects an unauthenticated browser to; API clients skip it
# entirely and send `Authorization: Basic` instead.


def _safe_next(raw: str | None) -> str:
    """Where to send someone after a successful login.

    Only a path on this site is ever accepted. Anything absolute,
    scheme-relative (`//evil.example`) or otherwise not starting with a
    single `/` falls back to the search page — a login form that will
    redirect anywhere it is told is an open redirect, and this one takes
    that target straight from the query string.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None) -> HTMLResponse:
    """The sign-in page, in both modes.

    Google mode shows a button rather than bouncing straight to the OAuth
    flow. Auto-redirecting looks tidier and breaks sign-out: /logout clears
    the session, lands here, gets sent to Google, and Google's own still-live
    SSO session signs the user straight back in without a prompt — so the
    button appears to do nothing at all.
    """
    mode = auth.load_auth_mode()
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": None, "mode": mode},
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> Response:
    """Check the submitted credentials and, if they are right, start a
    session. A wrong password re-renders the form with one deliberately
    vague message — which of the two was wrong is not the visitor's
    business."""
    if auth.load_auth_mode() != "basic":
        raise HTTPException(status_code=404, detail="Not found")

    target = _safe_next(next)
    if not auth.check_basic_credentials(username, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": target, "error": "Incorrect username or password.", "mode": "basic"},
            status_code=401,
        )

    request.session["user"] = username
    return RedirectResponse(url=target, status_code=303)


@app.post("/logout")
def logout_submit(request: Request) -> RedirectResponse:
    """POST, not GET: a link a browser can prefetch should not be able to
    sign someone out.

    Works in both modes. Note this ends the KaBOM session only — it cannot
    end the visitor's Google session, which is why /login shows a button in
    google mode rather than redirecting: otherwise signing out would bounce
    through Google's still-live SSO and sign them straight back in.
    """
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# --- Google OAuth entry points -----------------------------------------------
#
# These three routes are the only ones that must stay reachable without
# already being authenticated — that is the entire point of a login flow —
# so they are registered directly on `app`, not on `protected`. Each still
# refuses to do anything unless KABOM_AUTH=google is actually the configured
# mode, rather than assuming a client would only ever call them in that
# mode.
#
# There is no live Google project wired up for this repo (no real client
# ID/secret exists to test against), so this is authlib's standard
# authorization-code + OpenID Connect flow, implemented against authlib's
# documented API and exercised in tests by monkeypatching the registered
# client's `authorize_access_token` — the same thing a real callback receives
# back from authlib after it has already verified the ID token's signature,
# issuer and nonce against Google's JWKS (see kabom/auth.py's
# get_google_oauth_client) — never against a live Google endpoint.


def _require_google_mode() -> None:
    if auth.load_auth_mode() != "google":
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/auth/login")
async def login(request: Request) -> RedirectResponse:
    """Kick off the standard OAuth2 authorization-code flow. authlib stores
    state + a nonce (since our scope includes "openid") in the session for
    /auth/callback to check on the way back."""
    _require_google_mode()
    client = auth.get_google_oauth_client()
    redirect_uri = request.url_for("auth_callback")
    return await client.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request) -> RedirectResponse:
    """Complete the flow: exchange the code for a token, verify the ID
    token (authlib does this internally against Google's JWKS — signature,
    issuer, nonce, expiry), then apply the two checks that actually matter
    here: `email_verified` must be true, and the email must be in the
    explicit KABOM_ALLOWED_EMAILS allow-list. Anything else is a plain 403,
    logged without the token/claims themselves."""
    _require_google_mode()
    client = auth.get_google_oauth_client()
    try:
        token = await client.authorize_access_token(request)
    except OAuthError:
        logger.warning("Google OAuth exchange failed")
        raise HTTPException(status_code=401, detail="Google sign-in failed") from None

    claims = token.get("userinfo")
    if not claims:
        logger.warning("Google OAuth token carried no verified ID token claims")
        raise HTTPException(status_code=401, detail="Google sign-in failed")

    if not claims.get("email_verified", False):
        logger.warning("Rejected Google sign-in: email not verified")
        raise HTTPException(status_code=403, detail="Email not verified")

    email = (claims.get("email") or "").strip().lower()
    config = auth.load_google_auth_config()
    if not email or email not in config.allowed_emails:
        # Deliberately no email in this log line — see the ticket's "no
        # secret value" bar; an email address is not a secret, but there is
        # no need to persist rejected addresses in the log either.
        logger.warning("Rejected Google sign-in: address not in the allow-list")
        raise HTTPException(status_code=403, detail="Not authorized")

    request.session["email"] = email
    return RedirectResponse(url="/")


@app.get("/auth/logout")
async def logout(request: Request) -> RedirectResponse:
    _require_google_mode()
    request.session.clear()
    return RedirectResponse(url="/")
