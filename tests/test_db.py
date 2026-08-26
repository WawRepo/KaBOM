"""Tests for kabom.db — schema and transactional ingest (HOME-230).

Runs against a fake S3 (moto) seeded with the committed sample files, same
pattern as tests/test_ingest.py — never a live MinIO, no real credentials.
The database is a fresh temporary file per test (pytest's tmp_path), never
:memory:, so these tests also exercise the real sqlite3-on-disk path.

Note on the "under 5 seconds on a Pi" acceptance criterion: this machine is
not a Raspberry Pi, so nothing here claims to benchmark that. These tests
only prove correctness (idempotency, transactional rollback, run bookkeeping).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from moto import mock_aws

from kabom import db
from kabom.config import S3Config
from kabom.s3_client import S3UnavailableError, build_client

SAMPLES_DIR = Path(__file__).parent / "samples"
REAL_SYFT_SAMPLE = SAMPLES_DIR / "syft-alpine-3.19.cdx.json"
CORRUPTED_SAMPLE = SAMPLES_DIR / "syft-alpine-3.19.corrupted.cdx.json"

TEST_BUCKET = "kabom-test-bucket"
TEST_CONFIG = S3Config(
    # moto's mock_aws only intercepts requests to AWS's own endpoint
    # patterns, so tests point here rather than at a MinIO-shaped URL. The
    # access key/secret are moto-only fakes; nothing here ever leaves the
    # process or hits a real network.
    endpoint="https://s3.amazonaws.com",
    bucket=TEST_BUCKET,
    access_key="fake-access-key",
    secret_key="fake-secret-key",
)

UNREACHABLE_CONFIG = S3Config(
    endpoint="http://127.0.0.1:1",
    bucket="any-bucket",
    access_key="fake",
    secret_key="fake",
)


def _seed_two_good_files(client) -> None:
    """Two distinct source keys, both parseable, both under hosts/ — enough
    sboms to make a "failure on the second one" test meaningful."""
    client.create_bucket(Bucket=TEST_BUCKET)
    client.put_object(
        Bucket=TEST_BUCKET,
        Key="hosts/alpine-a.cdx.json",
        Body=REAL_SYFT_SAMPLE.read_bytes(),
    )
    client.put_object(
        Bucket=TEST_BUCKET,
        Key="hosts/alpine-b.cdx.json",
        Body=REAL_SYFT_SAMPLE.read_bytes(),
    )


def _seed_good_and_corrupted(client) -> None:
    client.create_bucket(Bucket=TEST_BUCKET)
    client.put_object(
        Bucket=TEST_BUCKET,
        Key="hosts/alpine-good.cdx.json",
        Body=REAL_SYFT_SAMPLE.read_bytes(),
    )
    client.put_object(
        Bucket=TEST_BUCKET,
        Key="hosts/alpine-corrupted.cdx.json",
        Body=CORRUPTED_SAMPLE.read_bytes(),
    )


def _counts(conn) -> tuple[int, int, int]:
    (sbom_count,) = conn.execute("SELECT COUNT(*) FROM sbom").fetchone()
    (component_count,) = conn.execute("SELECT COUNT(*) FROM component").fetchone()
    (run_count,) = conn.execute("SELECT COUNT(*) FROM run").fetchone()
    return sbom_count, component_count, run_count


@pytest.fixture
def conn(tmp_path):
    connection = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(connection)
    yield connection
    connection.close()


# --- schema -----------------------------------------------------------


def test_init_db_creates_the_three_tables(conn):
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"sbom", "component", "run"} <= tables


def test_init_db_is_safe_to_call_twice(conn):
    db.init_db(conn)  # must not raise


def test_component_name_is_indexed(conn):
    indexes = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }
    assert "idx_component_name" in indexes


# --- infer_kind ---------------------------------------------------------


def test_infer_kind_from_hosts_prefix():
    assert db.infer_kind("hosts/alpine.cdx.json") == "host"


def test_infer_kind_from_images_prefix():
    assert db.infer_kind("images/nginx.cdx.json") == "image"


# --- run_ingest: basic write ---------------------------------------------


@mock_aws
def test_run_ingest_writes_sboms_components_and_a_run_row(conn):
    client = build_client(TEST_CONFIG)
    _seed_good_and_corrupted(client)

    summary = db.run_ingest(conn, TEST_CONFIG)

    assert summary.ok is True
    assert summary.sboms_seen == 2
    assert summary.sboms_failed == 1

    sbom_count, component_count, run_count = _counts(conn)
    assert sbom_count == 1
    assert component_count == 96
    assert run_count == 1

    (ok, sboms_seen, sboms_failed) = conn.execute(
        "SELECT ok, sboms_seen, sboms_failed FROM run"
    ).fetchone()
    assert ok == 1
    assert sboms_seen == 2
    assert sboms_failed == 1


@mock_aws
def test_run_ingest_records_kind_and_generated_at(conn):
    client = build_client(TEST_CONFIG)
    _seed_good_and_corrupted(client)

    db.run_ingest(conn, TEST_CONFIG)

    subject, kind, generated_at, source_key = conn.execute(
        "SELECT subject, kind, generated_at, source_key FROM sbom"
    ).fetchone()
    assert subject == "alpine"
    assert kind == "host"
    assert generated_at == "2026-08-26T10:47:17+02:00"
    assert source_key == "hosts/alpine-good.cdx.json"


# --- acceptance: idempotent -----------------------------------------------


@mock_aws
def test_ingesting_twice_gives_identical_row_counts(conn):
    client = build_client(TEST_CONFIG)
    _seed_two_good_files(client)

    db.run_ingest(conn, TEST_CONFIG)
    first_counts = _counts(conn)

    db.run_ingest(conn, TEST_CONFIG)
    second_counts = _counts(conn)

    # sbom/component counts unchanged (no duplicates); run count grew by one.
    assert first_counts[0] == second_counts[0] == 2
    assert first_counts[1] == second_counts[1] == 192
    assert second_counts[2] == first_counts[2] + 1


# --- acceptance: interrupted ingest leaves previous data intact -----------


@mock_aws
def test_interrupted_ingest_leaves_previous_contents_queryable(conn, monkeypatch):
    client = build_client(TEST_CONFIG)
    _seed_two_good_files(client)

    # Baseline: one full, successful ingest.
    db.run_ingest(conn, TEST_CONFIG)
    baseline_sbom_count, baseline_component_count, baseline_run_count = _counts(conn)
    baseline_rows = conn.execute("SELECT source_key FROM sbom ORDER BY source_key").fetchall()
    assert baseline_sbom_count == 2
    assert baseline_component_count == 192

    # Now make the write fail partway through the second sbom, simulating an
    # ingest interrupted mid-transaction.
    real_infer_kind = db.infer_kind
    calls = {"n": 0}

    def flaky_infer_kind(source_key: str) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure partway through the write")
        return real_infer_kind(source_key)

    monkeypatch.setattr(db, "infer_kind", flaky_infer_kind)

    with pytest.raises(RuntimeError, match="simulated failure"):
        db.run_ingest(conn, TEST_CONFIG)

    # The previous contents must be intact and queryable — not half-replaced.
    sbom_count, component_count, run_count = _counts(conn)
    assert sbom_count == baseline_sbom_count
    assert component_count == baseline_component_count
    rows = conn.execute("SELECT source_key FROM sbom ORDER BY source_key").fetchall()
    assert rows == baseline_rows

    # And the failed attempt is recorded, not silently dropped.
    assert run_count == baseline_run_count + 1
    (ok,) = conn.execute("SELECT ok FROM run ORDER BY id DESC LIMIT 1").fetchone()
    assert ok == 0


# --- acceptance: failed run recorded with ok = false ----------------------


def test_s3_unavailable_records_failed_run_and_leaves_db_untouched(conn):
    # moto's mock_aws patches botocore itself, so it would happily intercept
    # even the "unreachable" client below and answer as if it were real S3.
    # Scope the mock tightly to the baseline setup, then step outside it so
    # the unreachable call hits a real (refused) socket connection — same
    # approach as test_ingest.py's test_list_object_keys_raises_when_bucket_unreachable.
    with mock_aws():
        client = build_client(TEST_CONFIG)
        _seed_two_good_files(client)
        db.run_ingest(conn, TEST_CONFIG)

    baseline_sbom_count, baseline_component_count, baseline_run_count = _counts(conn)

    with pytest.raises(S3UnavailableError):
        db.run_ingest(conn, UNREACHABLE_CONFIG)

    sbom_count, component_count, run_count = _counts(conn)
    assert sbom_count == baseline_sbom_count
    assert component_count == baseline_component_count
    assert run_count == baseline_run_count + 1

    (ok, sboms_seen, sboms_failed) = conn.execute(
        "SELECT ok, sboms_seen, sboms_failed FROM run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ok == 0
    assert sboms_seen == 0
    assert sboms_failed == 0
