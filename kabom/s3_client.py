"""Thin, read-only S3 (MinIO) access.

KaBOM never writes to the bucket, ever. This module only lists and fetches objects.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from kabom.config import S3Config


class S3UnavailableError(RuntimeError):
    """Raised when MinIO cannot be reached or a request otherwise fails.

    Callers must let this surface as a clear failure, never fall back to
    serving cached/stale data as if it were live — see the parent ticket's
    "never show stale data as though it were current."
    """


def build_client(config: S3Config):
    """Build a boto3 S3 client for the configured MinIO endpoint.

    MinIO ignores the region, but boto3's S3 client requires one to be set;
    "us-east-1" is a fixed, meaningless placeholder here, not a real AWS
    region selection.
    """
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name="us-east-1",
    )


def list_object_keys(client, bucket: str) -> Iterator[str]:
    """Yield every object key in the bucket.

    Raises S3UnavailableError if MinIO cannot be reached or the bucket
    listing otherwise fails.
    """
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                yield obj["Key"]
    except (BotoCoreError, ClientError) as exc:
        raise S3UnavailableError(f"Could not list objects in bucket {bucket!r}: {exc}") from exc


def get_object_bytes(client, bucket: str, key: str) -> bytes:
    """Fetch one object's full content.

    Raises S3UnavailableError if MinIO cannot be reached or the fetch
    otherwise fails.
    """
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        raise S3UnavailableError(
            f"Could not fetch object {key!r} from bucket {bucket!r}: {exc}"
        ) from exc
