"""S3 (MinIO) configuration.

Config comes from environment variables only. No credentials in code, in
defaults, or in tests — see CLAUDE.md's "Secrets in env vars" trap and
HOME-229's "no credentials in code, in defaults, or in tests" requirement.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_REQUIRED_VARS = (
    "KABOM_S3_ENDPOINT",
    "KABOM_S3_BUCKET",
    "KABOM_S3_ACCESS_KEY",
    "KABOM_S3_SECRET_KEY",
)


@dataclass(frozen=True)
class S3Config:
    """Connection details for the read-only MinIO bucket KaBOM reads from."""

    endpoint: str
    bucket: str
    access_key: str
    secret_key: str


def load_s3_config() -> S3Config:
    """Build an S3Config from the environment.

    Raises ValueError naming every missing variable if any are unset — fail
    clearly at startup rather than limping along with a partial config.
    """
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in _REQUIRED_VARS:
        value = os.environ.get(name)
        if not value:
            missing.append(name)
        else:
            values[name] = value

    if missing:
        raise ValueError(
            "Missing required S3 configuration environment variable(s): " + ", ".join(missing)
        )

    return S3Config(
        endpoint=values["KABOM_S3_ENDPOINT"],
        bucket=values["KABOM_S3_BUCKET"],
        access_key=values["KABOM_S3_ACCESS_KEY"],
        secret_key=values["KABOM_S3_SECRET_KEY"],
    )
