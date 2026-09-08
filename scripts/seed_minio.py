#!/usr/bin/env python3
"""Seed the dev/test MinIO bucket used by docker-compose.yml with sample
CycloneDX files.

This is dev/test tooling only — see docker-compose.yml's `seed` service. It
runs against the compose-local MinIO container, never your real MinIO,
and it is never copied into the production image (the Dockerfile only ever
COPYs the `kabom` package; this script, dev-sboms/ and tests/samples/ are
bind-mounted into the `seed` container by docker-compose instead).

Reuses the app's own boto3 dependency and the same image built from the
production Dockerfile, rather than pulling in a second `mc`/`jq`-based
image — the rest of this project is Python, and a second toolchain for one
script is a tax paid forever.

Everything in dev-sboms/ is uploaded, so dropping your own real Syft output
in there is all it takes to browse it locally — no code change needed.

KABOM_SEED_SCENARIO picks the freshness the banner ends up showing
(default "mixed"):

  mixed  (default) - every dev-sboms/ file, plus the deliberately corrupted
                     fixture from tests/samples/. One file that cannot be
                     parsed forces a RED banner regardless of age, so a
                     plain `docker compose up` demonstrates KaBOM's real
                     failure-reporting out of the box instead of hiding it
                     behind a flag nobody would think to pass.
  fresh  - every dev-sboms/ file, timestamps rewritten to now -> GREEN.
  amber  - every dev-sboms/ file, timestamps rewritten to 3 days ago -> AMBER.

`fresh` and `amber` rewrite timestamps rather than trusting what Syft wrote,
because these files are committed: their real generation dates recede into
the past, and a fixture that silently drifts from GREEN to RED over a few
months would break the Playwright suite (tests/e2e/) long after the commit
that "caused" it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEV_SBOMS_DIR = _REPO_ROOT / "dev-sboms"

# The one file deliberately kept in tests/samples/ rather than dev-sboms/: it
# is a pytest fixture first (tests/test_ingest.py, tests/test_db.py) and only
# incidentally useful here, and dev-sboms/ is documented as "drop a real SBOM
# in and it shows up" — a broken file sitting in there invites confusion.
_CORRUPTED_SAMPLE = _REPO_ROOT / "tests" / "samples" / "syft-alpine-3.19.corrupted.cdx.json"


def _dev_sbom_files() -> list[Path]:
    files = sorted(_DEV_SBOMS_DIR.glob("*.json"))
    if not files:
        raise SystemExit(f"No *.json files found in {_DEV_SBOMS_DIR} — nothing to seed.")
    return files


def _key_for(path: Path) -> str:
    """S3 key for one local file.

    The leading path segment is what kabom.db.infer_kind reads to decide
    whether an SBOM describes a container image or a host, so everything in
    dev-sboms/ lands under `images/` — these are all `syft <image>` output.
    """
    return f"images/{path.name}"


def _body(path: Path, timestamp: datetime | None) -> bytes:
    """One file's bytes, optionally with metadata.timestamp rewritten so a
    scenario can control the sbom's age without a second set of fixtures."""
    raw = path.read_bytes()
    if timestamp is None:
        return raw
    doc = json.loads(raw)
    doc.setdefault("metadata", {})["timestamp"] = timestamp.isoformat()
    return json.dumps(doc).encode("utf-8")


def _objects_for_scenario(scenario: str) -> list[tuple[str, bytes]]:
    if scenario == "fresh":
        stamp = datetime.now(UTC)
    elif scenario == "amber":
        stamp = datetime.now(UTC) - timedelta(days=3)
    elif scenario == "mixed":
        stamp = datetime.now(UTC)
    else:
        raise ValueError(f"Unknown KABOM_SEED_SCENARIO: {scenario!r}")

    objects = [(_key_for(path), _body(path, stamp)) for path in _dev_sbom_files()]

    if scenario == "mixed":
        # Appended last so the good files above are unambiguously fresh: the
        # banner goes RED here because one file failed to parse, not because
        # the data is old. Keeps the Playwright assertion meaningful.
        objects.append((f"images/{_CORRUPTED_SAMPLE.name}", _CORRUPTED_SAMPLE.read_bytes()))

    return objects


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
