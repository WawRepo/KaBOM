# KaBOM

*"Your SBOMs, without the enterprise."*

KaBOM is a small web app that reads CycloneDX SBOMs (Software Bill of
Materials) out of MinIO (an S3-compatible object store) and lets one person
search them. It is built for a 12-node Raspberry Pi homelab with 27 SBOM
files, not for a company with thousands. It does not scan anything — Grype
does that separately, and KaBOM only ever reads what that job produces.

## Development

```bash
uv sync                  # install dependencies
uv run pytest            # run tests
uv run ruff check .      # lint
uv run ruff format .     # format
uv run uvicorn kabom.main:app --reload    # dev server
```

## Status

This is the repo skeleton (HOME-228). No database, no S3 client, no auth, and
no UI yet — those land in follow-up tickets. Today the app serves one route:

```
GET /healthz -> {"status": "ok"}
```
