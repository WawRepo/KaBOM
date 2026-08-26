"""FastAPI application entrypoint.

This is intentionally a skeleton: no database, no S3 client, no auth, no UI.
Those arrive in later tickets (HOME-229, HOME-230, HOME-231, HOME-232,
HOME-233). See HOME-228.
"""

from fastapi import FastAPI

app = FastAPI(title="KaBOM")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness check. Always returns ok if the process is up."""
    return {"status": "ok"}
