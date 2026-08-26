"""Fetch every SBOM out of the configured bucket and parse it.

This is the read half of HOME-229: list the bucket, fetch each object, parse
it as CycloneDX, and hand back everything that parsed plus a countable,
named record of everything that didn't. No database yet — that's HOME-230.
This module ends with parsed objects in memory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kabom.config import S3Config
from kabom.cyclonedx import CycloneDXParseError, ParsedSBOM, parse_cyclonedx
from kabom.s3_client import build_client, get_object_bytes, list_object_keys

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestError:
    """One file that could not be used, named so it can be shown, not hidden."""

    key: str
    reason: str


@dataclass
class IngestResult:
    """Everything produced by one ingest pass."""

    sboms: list[ParsedSBOM] = field(default_factory=list)
    errors: list[IngestError] = field(default_factory=list)


def ingest_all(config: S3Config) -> IngestResult:
    """List and parse every object in the configured bucket.

    A malformed or truncated file is skipped, counted, and logged by name —
    it never blanks the whole result and never vanishes silently. MinIO being
    unreachable is not caught here: it propagates as S3UnavailableError so
    callers fail clearly instead of serving stale data as current.
    """
    client = build_client(config)
    result = IngestResult()

    for key in list_object_keys(client, config.bucket):
        try:
            raw = get_object_bytes(client, config.bucket, key)
            sbom = parse_cyclonedx(raw, key)
        except CycloneDXParseError as exc:
            logger.warning("Skipping unparseable SBOM %s: %s", key, exc)
            result.errors.append(IngestError(key=key, reason=str(exc)))
            continue
        result.sboms.append(sbom)

    return result
