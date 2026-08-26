#!/usr/bin/env python3
"""Seed the dev/test MinIO bucket used by docker-compose.yml with sample
CycloneDX files.

This is dev/test tooling only — see docker-compose.yml's `seed` service. It
runs against the compose-local MinIO container, never the real storage-host MinIO,
and it is never copied into the production image (the Dockerfile only ever
COPYs the `kabom` package; this script and tests/samples/ are bind-mounted
into the `seed` container by docker-compose instead).

Reuses the app's own boto3 dependency and the same image built from the
production Dockerfile, rather than pulling in a second `mc`/`jq`-based
image — see CLAUDE.md: "every script in the homelab is Python."

KABOM_SEED_SCENARIO picks what gets uploaded (default "mixed"):

  mixed  (default) - the two committed sample files exactly as they are: one
                      good, one corrupted. This is deliberately the default
                      so a plain `docker compose up` demonstrates KaBOM's
                      real failure-reporting behaviour out of the box (a RED
                      banner, "1 of 2 read") instead of hiding it behind a
                      flag nobody would think to pass.
  fresh  - only the good sample, timestamp untouched -> a GREEN banner.
  amber  - only the good sample, timestamp rewritten to 3 days ago -> AMBER.

Used by the Playwright suite (tests/e2e/) to exercise all three freshness-
banner colours — see the README for how the suite cycles through scenarios.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

_SAMPLES_DIR = Path(__file__).resolve().parent.parent / "tests" / "samples"
_GOOD_SAMPLE = _SAMPLES_DIR / "syft-alpine-3.19.cdx.json"
_CORRUPTED_SAMPLE = _SAMPLES_DIR / "syft-alpine-3.19.corrupted.cdx.json"


def _good_bytes(timestamp: datetime | None) -> bytes:
    """The good sample's bytes, optionally with metadata.timestamp rewritten
    so a scenario can control the sbom's age without a second fixture."""
    doc = json.loads(_GOOD_SAMPLE.read_text())
    if timestamp is not None:
        doc["metadata"]["timestamp"] = timestamp.isoformat()
    return json.dumps(doc).encode("utf-8")


def _objects_for_scenario(scenario: str) -> list[tuple[str, bytes]]:
    if scenario == "fresh":
        return [("hosts/alpine-good.cdx.json", _good_bytes(None))]
    if scenario == "amber":
        amber_timestamp = datetime.now(UTC) - timedelta(days=3)
        return [("hosts/alpine-good.cdx.json", _good_bytes(amber_timestamp))]
    if scenario == "mixed":
        return [
            ("hosts/alpine-good.cdx.json", _GOOD_SAMPLE.read_bytes()),
            ("hosts/alpine-corrupted.cdx.json", _CORRUPTED_SAMPLE.read_bytes()),
        ]
    raise ValueError(f"Unknown KABOM_SEED_SCENARIO: {scenario!r}")


def _wait_for_minio(client, attempts: int = 30, delay_seconds: float = 1.0) -> None:
    """Retry until MinIO answers, instead of relying on a container
    healthcheck — keeps docker-compose.yml simple (see its comments)."""
    for attempt in range(attempts):
        try:
            client.list_buckets()
            return
        except (BotoCoreError, ClientError):
            if attempt == attempts - 1:
                raise
            time.sleep(delay_seconds)


def _ensure_bucket(client, bucket: str) -> None:
    try:
        client.create_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise


def main() -> None:
    scenario = os.environ.get("KABOM_SEED_SCENARIO", "mixed")
    endpoint = os.environ["KABOM_S3_ENDPOINT"]
    bucket = os.environ["KABOM_S3_BUCKET"]
    access_key = os.environ["KABOM_S3_ACCESS_KEY"]
    secret_key = os.environ["KABOM_S3_SECRET_KEY"]

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
    )

    _wait_for_minio(client)
    _ensure_bucket(client, bucket)

    for key, body in _objects_for_scenario(scenario):
        client.put_object(Bucket=bucket, Key=key, Body=body)
        print(f"seeded {key} ({len(body)} bytes)")

    print(f"seed scenario {scenario!r} complete")


if __name__ == "__main__":
    main()
