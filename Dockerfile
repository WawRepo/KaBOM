# --- Stage 1: compile Tailwind CSS with the *standalone* CLI ---------------
#
# The standalone CLI is a single downloaded binary, not an npm package — no
# node, no node_modules, anywhere in this build. This stage exists purely to
# produce kabom/static/css/style.css; it is discarded after that, so nothing
# it installs (curl, the binary itself) ever reaches the runtime image below.
# See CLAUDE.md and HOME-232: "No node_modules in the runtime image."
FROM debian:bookworm-slim AS tailwind

ARG TAILWIND_VERSION=v4.3.3
# Set automatically by BuildKit to the target platform's arch (amd64/arm64)
# — this is what makes the multi-arch build in CLAUDE.md's traps table work
# without hand-picking a binary.
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
    curl -sL -o /usr/local/bin/tailwindcss \
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

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "kabom.main:app", "--host", "0.0.0.0", "--port", "8000"]
