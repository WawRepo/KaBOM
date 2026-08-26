"""Tests for kabom.ingest / kabom.cyclonedx / kabom.s3_client.

Everything here runs against a fake S3 (moto) seeded with the committed
sample files in tests/samples/ — never a live MinIO. No network, no real
credentials: the fake access key/secret below are moto-only and are rejected
by any real AWS or MinIO endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from kabom.config import S3Config, load_s3_config
from kabom.cyclonedx import CycloneDXParseError, parse_cyclonedx
from kabom.ingest import ingest_all
from kabom.s3_client import S3UnavailableError, build_client, get_object_bytes, list_object_keys

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


def _seed_bucket(client) -> None:
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


# --- kabom.cyclonedx -------------------------------------------------------


def test_parse_real_syft_sample_returns_expected_component_count():
    raw = REAL_SYFT_SAMPLE.read_bytes()
    sbom = parse_cyclonedx(raw, "hosts/alpine-good.cdx.json")

    assert len(sbom.components) == 96
    assert sbom.subject_name == "alpine"
    assert sbom.generated_at is not None
    assert sbom.generated_at.isoformat() == "2026-08-26T10:47:17+02:00"


def test_parse_real_syft_sample_extracts_name_version_type_purl():
    raw = REAL_SYFT_SAMPLE.read_bytes()
    sbom = parse_cyclonedx(raw, "hosts/alpine-good.cdx.json")

    by_name = {c.name: c for c in sbom.components}
    baselayout = by_name["alpine-baselayout"]
    assert baselayout.version == "3.4.3-r2"
    assert baselayout.type == "library"
    assert baselayout.purl == (
        "pkg:apk/alpine/alpine-baselayout@3.4.3-r2?arch=aarch64&distro=alpine-3.19.9"
    )


def test_parse_corrupted_sample_raises():
    raw = CORRUPTED_SAMPLE.read_bytes()
    with pytest.raises(CycloneDXParseError):
        parse_cyclonedx(raw, "hosts/alpine-corrupted.cdx.json")


def test_component_missing_version_and_purl_is_still_parsed():
    # File-type components in the real sample (e.g. /etc/hosts) have no
    # version or purl — that must not blow up parsing.
    raw = REAL_SYFT_SAMPLE.read_bytes()
    sbom = parse_cyclonedx(raw, "hosts/alpine-good.cdx.json")

    file_components = [c for c in sbom.components if c.type == "file"]
    assert file_components
    assert any(c.version is None and c.purl is None for c in file_components)


def test_missing_metadata_timestamp_is_none_not_fresh():
    doc = json.loads(REAL_SYFT_SAMPLE.read_text())
    del doc["metadata"]["timestamp"]
    raw = json.dumps(doc).encode("utf-8")

    sbom = parse_cyclonedx(raw, "hosts/no-timestamp.cdx.json")

    assert sbom.generated_at is None


def test_wrong_bom_format_is_rejected():
    raw = b'{"bomFormat": "SPDX", "components": []}'
    with pytest.raises(CycloneDXParseError):
        parse_cyclonedx(raw, "hosts/not-cyclonedx.json")


def test_invalid_json_is_rejected():
    with pytest.raises(CycloneDXParseError):
        parse_cyclonedx(b"{not json at all", "hosts/garbage.json")


# --- kabom.config ------------------------------------------------------


def test_load_s3_config_raises_when_env_vars_missing(monkeypatch):
    for var in (
        "KABOM_S3_ENDPOINT",
        "KABOM_S3_BUCKET",
        "KABOM_S3_ACCESS_KEY",
        "KABOM_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ValueError, match="KABOM_S3_ENDPOINT"):
        load_s3_config()


def test_load_s3_config_reads_from_environment(monkeypatch):
    monkeypatch.setenv("KABOM_S3_ENDPOINT", "https://minio.example.internal")
    monkeypatch.setenv("KABOM_S3_BUCKET", "sboms")
    monkeypatch.setenv("KABOM_S3_ACCESS_KEY", "AK")
    monkeypatch.setenv("KABOM_S3_SECRET_KEY", "SK")

    config = load_s3_config()

    assert config.endpoint == "https://minio.example.internal"
    assert config.bucket == "sboms"
    assert config.access_key == "AK"
    assert config.secret_key == "SK"


# --- kabom.s3_client / kabom.ingest, against a fake S3 (moto) -------------


@mock_aws
def test_list_and_fetch_objects_from_fake_bucket():
    client = build_client(TEST_CONFIG)
    _seed_bucket(client)

    keys = sorted(list_object_keys(client, TEST_BUCKET))
    assert keys == ["hosts/alpine-corrupted.cdx.json", "hosts/alpine-good.cdx.json"]

    body = get_object_bytes(client, TEST_BUCKET, "hosts/alpine-good.cdx.json")
    assert body == REAL_SYFT_SAMPLE.read_bytes()


@mock_aws
def test_get_object_bytes_raises_for_missing_key():
    client = build_client(TEST_CONFIG)
    client.create_bucket(Bucket=TEST_BUCKET)

    with pytest.raises(S3UnavailableError):
        get_object_bytes(client, TEST_BUCKET, "does/not/exist.json")


def test_list_object_keys_raises_when_bucket_unreachable():
    # No @mock_aws here: boto3 talks to a fake endpoint with no bucket set up
    # and no fake AWS backend intercepting it, so it must fail clearly
    # rather than pretend there is nothing there.
    client = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:1",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
        region_name="us-east-1",
    )

    with pytest.raises(S3UnavailableError):
        list(list_object_keys(client, "any-bucket"))


@mock_aws
def test_ingest_all_returns_parsed_sboms_and_counts_the_corrupted_one():
    client = build_client(TEST_CONFIG)
    _seed_bucket(client)

    result = ingest_all(TEST_CONFIG)

    assert len(result.sboms) == 1
    assert result.sboms[0].source_key == "hosts/alpine-good.cdx.json"
    assert len(result.sboms[0].components) == 96

    assert len(result.errors) == 1
    assert result.errors[0].key == "hosts/alpine-corrupted.cdx.json"
    assert "invalid JSON" in result.errors[0].reason


@mock_aws
def test_ingest_all_with_only_good_files_has_no_errors():
    client = build_client(TEST_CONFIG)
    client.create_bucket(Bucket=TEST_BUCKET)
    client.put_object(
        Bucket=TEST_BUCKET,
        Key="hosts/alpine-good.cdx.json",
        Body=REAL_SYFT_SAMPLE.read_bytes(),
    )

    result = ingest_all(TEST_CONFIG)

    assert len(result.sboms) == 1
    assert result.errors == []
