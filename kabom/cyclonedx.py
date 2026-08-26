"""Parsing for CycloneDX SBOM documents.

KaBOM only ever reads CycloneDX JSON produced by Syft (HOME-224). This module
has no opinion about vulnerabilities or policy — it extracts exactly what the
search feature (HOME-231) and the freshness banner (HOME-232) need: the
subject, the generation timestamp, and the component list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime


class CycloneDXParseError(ValueError):
    """Raised when a file is not usable as a CycloneDX SBOM.

    Covers invalid JSON, JSON that is not a CycloneDX document, and any other
    shape KaBOM cannot make sense of. Callers should catch this, skip the
    file, log its name, and keep going — never let one bad file blank the
    whole view.
    """


@dataclass(frozen=True)
class Component:
    """One entry from an SBOM's component list."""

    name: str
    version: str | None
    type: str | None
    purl: str | None


@dataclass(frozen=True)
class ParsedSBOM:
    """The result of parsing one CycloneDX SBOM file.

    `generated_at` is None when the source document has no
    `metadata.timestamp` — that must be treated as stale, never as fresh, by
    anything downstream that computes data age (HOME-232's freshness banner).
    """

    source_key: str
    subject_name: str | None
    generated_at: datetime | None
    components: list[Component]


def parse_cyclonedx(raw: bytes, source_key: str) -> ParsedSBOM:
    """Parse raw bytes as a CycloneDX JSON SBOM.

    Raises CycloneDXParseError on anything that isn't a usable CycloneDX
    document: invalid JSON, wrong top-level shape, or a bomFormat other than
    CycloneDX.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CycloneDXParseError(f"{source_key}: not valid UTF-8: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CycloneDXParseError(f"{source_key}: invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise CycloneDXParseError(f"{source_key}: top level is not a JSON object")

    if data.get("bomFormat") != "CycloneDX":
        raise CycloneDXParseError(
            f"{source_key}: not a CycloneDX document (bomFormat={data.get('bomFormat')!r})"
        )

    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise CycloneDXParseError(f"{source_key}: metadata is not a JSON object")

    subject_name = None
    subject_component = metadata.get("component")
    if isinstance(subject_component, dict):
        subject_name = subject_component.get("name")

    generated_at = _parse_timestamp(metadata.get("timestamp"))

    raw_components = data.get("components") or []
    if not isinstance(raw_components, list):
        raise CycloneDXParseError(f"{source_key}: components is not a JSON array")

    components = [_parse_component(item) for item in raw_components if isinstance(item, dict)]

    return ParsedSBOM(
        source_key=source_key,
        subject_name=subject_name,
        generated_at=generated_at,
        components=components,
    )


def _parse_component(item: dict) -> Component:
    return Component(
        name=item.get("name") or "",
        version=item.get("version"),
        type=item.get("type"),
        purl=item.get("purl"),
    )


def _parse_timestamp(value: object) -> datetime | None:
    """Parse metadata.timestamp. Anything missing or unparseable is None,
    which callers must treat as stale — never as fresh."""
    if not isinstance(value, str) or not value:
        return None
    try:
        # CycloneDX timestamps are ISO 8601; Python's fromisoformat handles
        # the common "+02:00" offset form. Normalize a trailing "Z" (UTC)
        # since fromisoformat only accepts that on 3.11+ in some forms.
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
