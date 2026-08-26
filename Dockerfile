# --- Stage 1: compile Tailwind CSS with the *standalone* CLI ---------------
#
# The standalone CLI is a single downloaded binary, not an npm package — no
# node, no node_modules, anywhere in this build. This stage exists purely to
# produce kabom/static/css/style.css; it is discarded after that, so nothing
# it installs (curl, the binary itself) ever reaches the runtime image below.
# The runtime image must never contain node_modules.
FROM debian:bookworm-slim AS tailwind

ARG TAILWIND_VERSION=v4.3.3
# `curl -f` matters: without it a 404 or a redirect to an error page is
# written to the output file and chmod'd executable, and the build fails
# later with something unrelated instead of at the download.
#
# Set automatically by BuildKit to the target platform's arch (amd64/arm64)
# — this is what makes the multi-arch build work without hand-picking a
# binary.
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY kabom/static/css/input.css kabom/static/css/input.css
COPY kabom/templates kabom/templates

RUN set -eux; \
    case "$TARGETARCH" in \
        amd64) asset="tailwindcss-linux-x64" ;; \
        arm64) asset="tailwindcss-linux-arm64" ;; \
        *) echo "Unsupported TARGETARCH for the standalone Tailwind CLI: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/tailwindcss \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"; \
    chmod +x /usr/local/bin/tailwindcss

RUN tailwindcss --cwd /build -i kabom/static/css/input.css -o kabom/static/css/style.css --minify

# --- Stage 2: the runtime image ---------------------------------------------

FROM python:3.12-slim

# Install uv by copying the static binary from its official image — no pip
# bootstrap needed, and it works the same on amd64 and arm64.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy dependency files first so dependency install is cached separately
# from application code changes. README.md is required too — hatchling
# reads it as the package long description.
COPY pyproject.toml uv.lock* README.md ./
RUN uv sync --no-dev --no-install-project

COPY kabom ./kabom

# Overwrite whatever style.css was committed with the one just compiled from
# source, so the image never depends on someone having remembered to run
# scripts/build-css.sh before `docker build`.
COPY --from=tailwind /build/kabom/static/css/style.css ./kabom/static/css/style.css

RUN uv sync --no-dev

# --- HOME-234: non-root, read-only-root-filesystem friendly --------------
#
# The app only ever writes one thing to disk: the SQLite database file (see
# kabom/config.py's load_db_path and kabom/db.py — everything else it reads
# is either baked into the image or fetched from S3/MinIO). That means the
# runtime container needs exactly one writable path, not a writable root
# filesystem.
#
# `/data` is that path. It defaults KABOM_DB_PATH here rather than leaving
# it at kabom.sqlite3-relative-to-/app, so a container run with
# --read-only (or a k8s Pod with securityContext.readOnlyRootFilesystem:
# true) still has somewhere sane to put the database without the deployer
# having to know the app's internals — they only need to mount a writable
# volume at /data (e.g. `-v $(pwd)/data:/data` or a PVC in k8s; see
# README.md). /app itself, and everything else in the image, stays
# read-only at runtime.
#
# A fixed, non-root UID/GID (not "the next free one") so a k8s
# securityContext.runAsUser can pin the same identity outside the image if
# it ever needs to (e.g. matching a PVC's fsGroup).
RUN groupadd --gid 1000 kabom \
    && useradd --uid 1000 --gid kabom --home-dir /app --shell /usr/sbin/nologin kabom \
    && mkdir -p /data \
    && chown -R kabom:kabom /app /data

ENV KABOM_DB_PATH=/data/kabom.sqlite3

USER kabom

EXPOSE 8000

# Not `uv run` here: `uv run`, even with --no-sync, still tries to write to
# its cache/state dir (UV_CACHE_DIR, defaulting under $HOME) on every
# invocation, which fails under a read-only root filesystem (see the
# read-only-root check in README.md). Dependencies are already synced into
# /app/.venv by the `uv sync` above, so invoking that venv's uvicorn
# directly needs no writes anywhere but /data.
CMD ["/app/.venv/bin/uvicorn", "kabom.main:app", "--host", "0.0.0.0", "--port", "8000"]
