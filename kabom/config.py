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


# --- local app config (db path, refresh interval) --------------------------
#
# Unlike S3Config, these have sensible defaults and are not secrets, so they
# do not need the "fail loudly if unset" treatment above.

DEFAULT_DB_PATH = "kabom.sqlite3"
DEFAULT_REFRESH_MINUTES = 60


def load_db_path() -> str:
    """Where the SQLite database file lives.

    Defaults to a file in the working directory. Whatever path this points
    to, it must be on `local-path` storage, never `nas-smb` — SMB gives no
    POSIX locking and SQLite corrupts on it. See CLAUDE.md's traps table and
    schema.sql's header comment.
    """
    return os.environ.get("KABOM_DB_PATH") or DEFAULT_DB_PATH


def load_refresh_minutes() -> int:
    """How often the background task re-ingests from S3, in minutes."""
    value = os.environ.get("KABOM_REFRESH_MINUTES")
    if not value:
        return DEFAULT_REFRESH_MINUTES
    return int(value)
