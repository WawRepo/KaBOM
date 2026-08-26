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
RUN uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "kabom.main:app", "--host", "0.0.0.0", "--port", "8000"]
