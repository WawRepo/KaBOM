"""Tests for kabom.main — the search API (HOME-231).

Every test seeds the database through kabom.db's public functions
(get_connection, init_db, run_ingest) with hand-built ParsedSBOM/Component
objects — kabom.db.ingest_all is monkeypatched to return them so no test
ever talks to real or fake S3. tests/test_db.py and tests/test_ingest.py
already cover the moto-based S3 path; this file stays at the HTTP layer.

The database dependency (kabom.main.get_db_connection) is overridden per
test to point at a fresh temporary SQLite file (pytest's tmp_path), never
the app's configured KABOM_DB_PATH. The FastAPI app's lifespan (startup
ingest + background refresh timer) is never triggered here: TestClient only
runs it when used as a context manager, and these tests deliberately avoid
that so no test needs real KABOM_S3_* credentials just to answer a request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from kabom import db
from kabom.config import S3Config
from kabom.cyclonedx import Component, ParsedSBOM
from kabom.ingest import IngestResult
from kabom.main import app, get_db_connection

# Never used for a real connection — kabom.db.ingest_all is monkeypatched in
# every test that calls run_ingest, so this only satisfies run_ingest's
# type signature.
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
    def override_get_db_connection():
        yield conn

    app.dependency_overrides[get_db_connection] = override_get_db_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db_connection, None)


def seed(conn, sboms, monkeypatch) -> db.RunSummary:
    """Ingest hand-built ParsedSBOM objects into `conn` without touching S3."""
    monkeypatch.setattr(db, "ingest_all", lambda config: IngestResult(sboms=list(sboms)))
    return db.run_ingest(conn, DUMMY_CONFIG)


def make_sbom(source_key, subject, generated_at, components) -> ParsedSBOM:
    return ParsedSBOM(
        source_key=source_key,
        subject_name=subject,
        generated_at=generated_at,
        components=components,
    )


# --- GET /healthz — unchanged, no DB dependency -----------------------------


def test_healthz_still_returns_ok_with_no_db_wiring():
    # Deliberately uses a bare TestClient, no dependency override at all, to
    # prove healthz needs no database access.
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- GET /api/search ---------------------------------------------------------


def test_search_present_in_two_sboms_returns_both_with_own_subject_and_timestamp(
    client, conn, monkeypatch
):
    newer = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
    older = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    seed(
        conn,
        [
            make_sbom(
                "hosts/a.cdx.json",
                "host-a",
                newer,
                [Component(name="libwebp", version="1.3.1", type="library", purl="pkg:a")],
            ),
            make_sbom(
                "images/b.cdx.json",
                "image-b",
                older,
                [Component(name="libwebp", version="1.3.1", type="library", purl="pkg:b")],
            ),
        ],
        monkeypatch,
    )

    response = client.get("/api/search", params={"q": "libwebp"})

    assert response.status_code == 200
    results = response.json()
    assert len(results) == 2
    by_subject = {r["subject"]: r for r in results}
    assert set(by_subject) == {"host-a", "image-b"}
    assert by_subject["host-a"]["generated_at"] == newer.isoformat()
    assert by_subject["image-b"]["generated_at"] == older.isoformat()
    assert by_subject["host-a"]["kind"] == "host"
    assert by_subject["image-b"]["kind"] == "image"
    assert by_subject["host-a"]["purl"] == "pkg:a"


def test_search_is_case_insensitive_substring_match(client, conn, monkeypatch):
    now = datetime.now(UTC)
    seed(
        conn,
        [
            make_sbom(
                "hosts/a.cdx.json",
                "a",
                now,
                [Component(name="LibWebP", version="1.0", type="library", purl=None)],
            )
        ],
        monkeypatch,
    )

    response = client.get("/api/search", params={"q": "webp"})

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["name"] == "LibWebP"


def test_search_version_filter_is_exact(client, conn, monkeypatch):
    now = datetime.now(UTC)
    seed(
        conn,
        [
            make_sbom(
                "hosts/a.cdx.json",
                "a",
                now,
                [
                    Component(name="libwebp", version="1.3.1", type="library", purl="pkg:a@1.3.1"),
                    Component(name="libwebp", version="1.2.0", type="library", purl="pkg:a@1.2.0"),
                ],
            )
        ],
        monkeypatch,
    )

    response = client.get("/api/search", params={"q": "libwebp", "version": "1.3.1"})

    results = response.json()
    assert len(results) == 1
    assert results[0]["version"] == "1.3.1"


def test_search_absent_package_returns_empty_list_with_200(client, conn, monkeypatch):
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

    response = client.get("/api/search", params={"q": "package-that-does-not-exist"})

    assert response.status_code == 200
    assert response.json() == []


def test_search_absent_from_empty_database_returns_empty_list_with_200(client):
    response = client.get("/api/search", params={"q": "anything"})

    assert response.status_code == 200
    assert response.json() == []


# --- GET /api/sboms and /api/sboms/{id} -------------------------------------


def test_list_sboms_reports_subject_kind_generated_at_and_component_count(
    client, conn, monkeypatch
):
    now = datetime.now(UTC)
    seed(
        conn,
        [
            make_sbom(
                "hosts/a.cdx.json",
                "host-a",
                now,
                [
                    Component("curl", "8.0", "library", None),
                    Component("bash", "5.2", "library", None),
                ],
            ),
            make_sbom("images/b.cdx.json", "image-b", now, []),
        ],
        monkeypatch,
    )

    response = client.get("/api/sboms")

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    by_subject = {r["subject"]: r for r in rows}
    assert by_subject["host-a"]["kind"] == "host"
    assert by_subject["host-a"]["component_count"] == 2
    assert by_subject["host-a"]["generated_at"] == now.isoformat()
    assert by_subject["image-b"]["component_count"] == 0


def test_get_sbom_by_id_returns_full_component_list(client, conn, monkeypatch):
    now = datetime.now(UTC)
    seed(
        conn,
        [
            make_sbom(
                "hosts/a.cdx.json",
                "host-a",
                now,
                [Component("curl", "8.0", "library", "pkg:curl@8.0")],
            )
        ],
        monkeypatch,
    )
    sbom_id = client.get("/api/sboms").json()[0]["id"]

    response = client.get(f"/api/sboms/{sbom_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == "host-a"
    assert body["kind"] == "host"
    assert body["generated_at"] == now.isoformat()
    assert body["components"] == [
        {"name": "curl", "version": "8.0", "type": "library", "purl": "pkg:curl@8.0"}
    ]


def test_get_sbom_missing_id_returns_404(client):
    response = client.get("/api/sboms/999999")

    assert response.status_code == 404


# --- GET /api/status ---------------------------------------------------------


def test_status_with_no_runs_yet(client):
    response = client.get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert body["finished_at"] is None
    assert body["sboms_seen"] == 0
    assert body["sboms_failed"] == 0
    assert body["ok"] is False
    assert body["age_seconds"] is None


def test_status_reports_run_counts_and_ok(client, conn, monkeypatch):
    seed(
        conn,
        [make_sbom("hosts/a.cdx.json", "a", datetime.now(UTC), [])],
        monkeypatch,
    )

    response = client.get("/api/status")

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["sboms_seen"] == 1
    assert body["sboms_failed"] == 0
    assert body["finished_at"] is not None


def test_status_reports_the_oldest_sbom_age_not_newest_or_average(client, conn, monkeypatch):
    fresh_a = datetime.now(UTC) - timedelta(hours=1)
    fresh_b = datetime.now(UTC) - timedelta(hours=2)
    deliberately_old = datetime.now(UTC) - timedelta(days=40)
    seed(
        conn,
        [
            make_sbom("hosts/a.cdx.json", "a", fresh_a, []),
            make_sbom("hosts/b.cdx.json", "b", fresh_b, []),
            make_sbom("hosts/stale.cdx.json", "stale", deliberately_old, []),
        ],
        monkeypatch,
    )

    response = client.get("/api/status")

    body = response.json()
    assert response.status_code == 200
    expected = (datetime.now(UTC) - deliberately_old).total_seconds()
    # Within a few seconds of wall-clock slack for the test run itself.
    assert abs(body["age_seconds"] - expected) < 5
    # Sanity: nowhere near the ~1-2 hour age of the fresh sboms, and not the
    # average of all three either.
    assert body["age_seconds"] > timedelta(days=39).total_seconds()


def test_status_treats_missing_generated_at_as_unknown_not_fresh(client, conn, monkeypatch):
    seed(
        conn,
        [make_sbom("hosts/no-timestamp.cdx.json", "a", None, [])],
        monkeypatch,
    )

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["age_seconds"] is None


# --- POST /admin/refresh -----------------------------------------------------


def test_admin_refresh_triggers_ingest_and_reports_summary(client, conn, monkeypatch):
    monkeypatch.setenv("KABOM_S3_ENDPOINT", "https://s3.example.invalid")
    monkeypatch.setenv("KABOM_S3_BUCKET", "bucket")
    monkeypatch.setenv("KABOM_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("KABOM_S3_SECRET_KEY", "sk")
    monkeypatch.setattr(
        db,
        "ingest_all",
        lambda config: IngestResult(
            sboms=[make_sbom("hosts/a.cdx.json", "a", datetime.now(UTC), [])]
        ),
    )

    response = client.post("/admin/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["sboms_seen"] == 1
    assert body["sboms_failed"] == 0

    listing = client.get("/api/sboms").json()
    assert len(listing) == 1
    assert listing[0]["subject"] == "a"


def test_admin_refresh_reports_failure_and_keeps_previous_data(client, conn, monkeypatch):
    monkeypatch.setenv("KABOM_S3_ENDPOINT", "https://s3.example.invalid")
    monkeypatch.setenv("KABOM_S3_BUCKET", "bucket")
    monkeypatch.setenv("KABOM_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("KABOM_S3_SECRET_KEY", "sk")

    # Baseline: one good sbom already ingested.
    seed(conn, [make_sbom("hosts/a.cdx.json", "a", datetime.now(UTC), [])], monkeypatch)

    from kabom.s3_client import S3UnavailableError

    def raise_unavailable(config):
        raise S3UnavailableError("simulated: MinIO unreachable")

    monkeypatch.setattr(db, "ingest_all", raise_unavailable)

    response = client.post("/admin/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "error" in body

    # Previous data must still be there.
    listing = client.get("/api/sboms").json()
    assert len(listing) == 1
    assert listing[0]["subject"] == "a"
