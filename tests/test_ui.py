"""Tests for kabom.main's UI routes and freshness banner (HOME-232).

Same pattern as tests/test_api.py: every test seeds through kabom.db's public
functions with hand-built ParsedSBOM/IngestError objects (kabom.db.ingest_all
is monkeypatched), never touching real or fake S3. These tests assert on the
rendered HTML directly rather than driving a real browser — that is what the
Playwright suite in tests/e2e/ is for (real typing/debounce, real htmx swaps,
real JS-blocked degradation, a real mobile viewport). This file exists so the
banner's colour thresholds, the "oldest first" sort, and the search grouping
are proven fast and in normal `pytest`/CI, without docker or a browser.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from kabom import db
from kabom.auth import require_auth
from kabom.config import S3Config
from kabom.cyclonedx import Component, ParsedSBOM
from kabom.ingest import IngestError, IngestResult
from kabom.main import app, get_db_connection

DUMMY_CONFIG = S3Config(
    endpoint="https://s3.example.invalid",
    bucket="unused",
    access_key="unused",
    secret_key="unused",
)


@pytest.fixture
def conn(tmp_path):
    connection = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(conn):
    """This file tests the UI screens/banner, not auth (HOME-233 owns
    that — see tests/test_auth.py), so `require_auth` is overridden away
    here the same way `get_db_connection` already is."""

    def override_get_db_connection():
        yield conn

    app.dependency_overrides[get_db_connection] = override_get_db_connection
    app.dependency_overrides[require_auth] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db_connection, None)
        app.dependency_overrides.pop(require_auth, None)


def make_sbom(source_key, subject, generated_at, components) -> ParsedSBOM:
    return ParsedSBOM(
        source_key=source_key,
        subject_name=subject,
        generated_at=generated_at,
        components=components,
    )


def seed(conn, sboms, monkeypatch, errors=()) -> db.RunSummary:
    """Ingest hand-built ParsedSBOM objects (and, optionally, IngestErrors to
    simulate files that failed to parse) into `conn` without touching S3."""
    monkeypatch.setattr(
        db, "ingest_all", lambda config: IngestResult(sboms=list(sboms), errors=list(errors))
    )
    return db.run_ingest(conn, DUMMY_CONFIG)


# --- The freshness banner ----------------------------------------------------


def test_banner_is_green_for_a_freshly_ingested_sbom(client, conn, monkeypatch):
    seed(conn, [make_sbom("hosts/a.cdx.json", "a", datetime.now(UTC), [])], monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert 'data-testid="freshness-banner"' in response.text
    assert 'data-level="green"' in response.text
    assert "Updated" in response.text
    assert "STALE" not in response.text


def test_banner_is_amber_between_one_day_and_seven_days_old(client, conn, monkeypatch):
    seed(
        conn,
        [make_sbom("hosts/a.cdx.json", "a", datetime.now(UTC) - timedelta(days=3), [])],
        monkeypatch,
    )

    response = client.get("/")

    assert 'data-level="amber"' in response.text
    assert "Updated 3 days ago" in response.text


def test_banner_is_red_when_oldest_sbom_is_over_seven_days_old(client, conn, monkeypatch):
    seed(
        conn,
        [make_sbom("hosts/a.cdx.json", "a", datetime.now(UTC) - timedelta(days=40), [])],
        monkeypatch,
    )

    response = client.get("/")

    assert 'data-level="red"' in response.text
    assert "STALE" in response.text
    assert "oldest data is 40 days old" in response.text
    assert "These answers may be wrong. Check the SBOM job." in response.text


def test_banner_is_red_when_a_file_failed_to_parse_even_if_fresh(client, conn, monkeypatch):
    seed(
        conn,
        [make_sbom("hosts/good.cdx.json", "good", datetime.now(UTC), [])],
        monkeypatch,
        errors=[IngestError(key="hosts/bad.cdx.json", reason="invalid JSON")],
    )

    response = client.get("/")

    assert 'data-level="red"' in response.text
    assert "1 of 2 read" in response.text


def test_banner_is_red_with_no_data_ingested_yet(client):
    response = client.get("/")

    assert 'data-level="red"' in response.text
    assert "STALE" in response.text


# --- The red border on search results, only when stale ----------------------


def test_search_results_get_red_border_when_banner_is_red(client, conn, monkeypatch):
    seed(
        conn,
        [
            make_sbom(
                "hosts/a.cdx.json",
                "a",
                datetime.now(UTC) - timedelta(days=40),
                [Component("curl", "8.0", "library", None)],
            )
        ],
        monkeypatch,
    )

    response = client.get("/", params={"q": "curl"})

    assert 'data-testid="search-results"' in response.text
    assert "border-red-600" in response.text


def test_search_results_have_no_red_border_when_banner_is_green(client, conn, monkeypatch):
    seed(
        conn,
        [
            make_sbom(
                "hosts/a.cdx.json",
                "a",
                datetime.now(UTC),
                [Component("curl", "8.0", "library", None)],
            )
        ],
        monkeypatch,
    )

    response = client.get("/", params={"q": "curl"})

    assert "border-red-600" not in response.text


# --- The search page: grouping by package name, keyboard-first form ---------


def test_search_groups_results_by_package_name_across_sboms(client, conn, monkeypatch):
    now = datetime.now(UTC)
    seed(
        conn,
        [
            make_sbom(
                "hosts/a.cdx.json",
                "host-a",
                now,
                [Component("libwebp", "1.3.1", "library", "pkg:a")],
            ),
            make_sbom(
                "images/b.cdx.json",
                "image-b",
                now,
                [Component("libwebp", "1.3.1", "library", "pkg:b")],
            ),
        ],
        monkeypatch,
    )

    response = client.get("/", params={"q": "libwebp"})

    assert response.status_code == 200
    # One group heading for the shared package name (a second, expected
    # occurrence is the search box echoing back its own value="libwebp"),
    # both subjects listed once each under it.
    assert response.text.count("libwebp") == 2
    assert response.text.count("host-a") == 1
    assert response.text.count("image-b") == 1


def test_index_page_has_a_plain_form_fallback_with_autofocused_input(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "<form" in response.text
    assert 'method="get"' in response.text
    assert 'action="/"' in response.text
    assert 'id="q"' in response.text
    assert "autofocus" in response.text
    assert 'type="submit"' in response.text


def test_index_page_with_no_query_shows_no_results_section(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "No matches for" not in response.text


def test_index_page_no_matches_message(client, conn, monkeypatch):
    seed(conn, [make_sbom("hosts/a.cdx.json", "a", datetime.now(UTC), [])], monkeypatch)

    response = client.get("/", params={"q": "nothing-like-this-exists"})

    assert "No match for" in response.text


# --- /sboms: sorted oldest first ---------------------------------------------


def _card_position(html: str, subject: str) -> int:
    """Where `subject`'s inventory card appears in the rendered page.

    Matches the subject as the card's own rendered text, so the assertion
    survives the surrounding markup changing shape — which it has, and
    ">subject<" did not.
    """
    match = re.search(rf">\s*{re.escape(subject)}\s*<", html)
    assert match is not None, f"no inventory card rendered for {subject!r}"
    return match.start()


def test_sboms_page_sorted_oldest_first(client, conn, monkeypatch):
    now = datetime.now(UTC)
    seed(
        conn,
        [
            make_sbom("hosts/newest.cdx.json", "newest", now - timedelta(hours=1), []),
            make_sbom("hosts/oldest.cdx.json", "oldest", now - timedelta(days=40), []),
            make_sbom("hosts/middle.cdx.json", "middle", now - timedelta(days=2), []),
        ],
        monkeypatch,
    )

    response = client.get("/sboms")

    assert response.status_code == 200
    # Anchor on each card's own link, not a bare substring match — the
    # page's instructional copy above the cards also contains the word
    # "oldest", and the subject text itself sits inside nested markup.
    positions = {
        subject: _card_position(response.text, subject)
        for subject in ("oldest", "middle", "newest")
    }
    assert positions["oldest"] < positions["middle"] < positions["newest"]


def test_sboms_page_treats_unknown_age_as_oldest(client, conn, monkeypatch):
    now = datetime.now(UTC)
    seed(
        conn,
        [
            make_sbom("hosts/known.cdx.json", "known", now - timedelta(days=1), []),
            make_sbom("hosts/unknown.cdx.json", "unknown-age", None, []),
        ],
        monkeypatch,
    )

    response = client.get("/sboms")

    assert _card_position(response.text, "unknown-age") < _card_position(response.text, "known")


# --- /sboms/{id}: full contents, filterable client-side ----------------------


def test_sbom_detail_page_lists_all_components(client, conn, monkeypatch):
    seed(
        conn,
        [
            make_sbom(
                "hosts/a.cdx.json",
                "host-a",
                datetime.now(UTC),
                [
                    Component("curl", "8.0", "library", "pkg:curl@8.0"),
                    Component("bash", "5.2", "library", None),
                ],
            )
        ],
        monkeypatch,
    )
    sbom_id = client.get("/api/sboms").json()[0]["id"]

    response = client.get(f"/sboms/{sbom_id}")

    assert response.status_code == 200
    assert "curl" in response.text
    assert "bash" in response.text
    assert 'id="component-filter"' in response.text


def test_sbom_detail_page_404_for_missing_id(client):
    response = client.get("/sboms/999999")

    assert response.status_code == 404


# --- Static assets ------------------------------------------------------------


def test_static_css_and_htmx_are_served():
    # Deliberately a bare TestClient, no lifespan/dependency override: static
    # files are served independent of the database or S3 configuration.
    client = TestClient(app)

    css = client.get("/static/css/style.css")
    htmx = client.get("/static/js/htmx.min.js")

    assert css.status_code == 200
    assert htmx.status_code == 200
