"""Tests for kabom.auth / the auth wiring in kabom.main (HOME-233).

Basic mode is exercised against the normal, already-imported `kabom.main`
app — same TestClient + dependency-override pattern as tests/test_api.py and
tests/test_ui.py, just leaving `require_auth` un-overridden so the real
check runs.

Google mode needs a bit more: kabom/main.py decides, once, at import time,
whether to attach Starlette's SessionMiddleware (and with what secret) —
see kabom/main.py's "HOME-233: auth" section. By the time this file runs,
`kabom.main` has almost certainly already been imported by another test file
under KABOM_AUTH unset, so it has no SessionMiddleware attached. The
`fresh_main` fixture below force-reimports kabom.main under whatever env
vars a given test has set, gets a genuinely fresh FastAPI `app` (and
therefore its own SessionMiddleware, its own dependency_overrides), and
restores the previously-cached module afterwards so later tests are
unaffected.

No test here talks to real Google infrastructure: KABOM_GOOGLE_CLIENT_ID/
SECRET below are obviously-fake placeholders, never dialled out to, and the
callback tests replace authlib's own `authorize_access_token` coroutine on
the registered client (its normal, documented per-app API) with a stub that
returns a token dict shaped like what authlib would hand back after already
verifying the ID token — so the callback route's own logic (email_verified,
the allow-list) is exercised for real, without a network call.
"""

from __future__ import annotations

import importlib
import sys

import bcrypt
import pytest
from fastapi.testclient import TestClient

from kabom import auth, db
from kabom.auth import require_auth
from kabom.main import app, get_db_connection

FAKE_USERNAME = "kabom-test-user"
FAKE_PASSWORD = "correct horse battery staple"
FAKE_PASSWORD_HASH = bcrypt.hashpw(FAKE_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

# Obviously-fake Google OAuth values — never a real client, never dialled
# out to. See the module docstring.
FAKE_GOOGLE_CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
FAKE_GOOGLE_CLIENT_SECRET = "fake-google-client-secret-not-real"  # noqa: S105
FAKE_SESSION_SECRET = "fake-session-secret-not-real"  # noqa: S105
ALLOWED_EMAIL = "allowed@example.com"
OUTSIDER_EMAIL = "outsider@example.com"

# Every route registered on kabom.main's `protected` router — the ticket's
# "every route except GET /healthz" list, restated here so the test that
# walks it is the one place that has to be kept in sync with kabom/main.py.
PROTECTED_ROUTES = [
    ("GET", "/", {}),
    ("GET", "/sboms", {}),
    ("GET", "/sboms/1", {}),
    ("GET", "/api/search", {"params": {"q": "curl"}}),
    ("GET", "/api/sboms", {}),
    ("GET", "/api/sboms/1", {}),
    ("GET", "/api/status", {}),
    ("POST", "/admin/refresh", {}),
]


def _basic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KABOM_AUTH", "basic")
    monkeypatch.setenv("KABOM_BASIC_USER", FAKE_USERNAME)
    monkeypatch.setenv("KABOM_BASIC_PASSWORD_HASH", FAKE_PASSWORD_HASH)
    # Defensive: even though auth should reject these requests before any
    # handler touches the database, point KABOM_DB_PATH at a throwaway file
    # rather than risk a stray kabom.sqlite3 landing in the repo root if
    # dependency-resolution order ever changes.
    monkeypatch.setenv("KABOM_DB_PATH", str(tmp_path / "kabom.sqlite3"))


# --- KABOM_AUTH=basic ---------------------------------------------------------


def test_healthz_needs_no_auth_even_in_basic_mode(monkeypatch, tmp_path):
    _basic_env(monkeypatch, tmp_path)

    response = TestClient(app).get("/healthz")

    assert response.status_code == 200


@pytest.mark.parametrize("method,path,kwargs", PROTECTED_ROUTES)
def test_every_protected_route_returns_401_without_credentials(
    monkeypatch, tmp_path, method, path, kwargs
):
    _basic_env(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.request(method, path, **kwargs)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


def test_wrong_password_is_rejected(monkeypatch, tmp_path):
    _basic_env(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.get("/api/status", auth=(FAKE_USERNAME, "not the password"))

    assert response.status_code == 401


def test_wrong_username_is_rejected(monkeypatch, tmp_path):
    _basic_env(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.get("/api/status", auth=("someone-else", FAKE_PASSWORD))

    assert response.status_code == 401


def test_correct_credentials_are_admitted(monkeypatch, tmp_path):
    _basic_env(monkeypatch, tmp_path)
    conn = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(conn)
    app.dependency_overrides[get_db_connection] = lambda: (yield conn)
    try:
        client = TestClient(app)

        response = client.get("/api/status", auth=(FAKE_USERNAME, FAKE_PASSWORD))

        assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_db_connection, None)
        conn.close()


def test_no_secret_value_appears_in_response_body_or_logs(monkeypatch, tmp_path, caplog):
    _basic_env(monkeypatch, tmp_path)
    client = TestClient(app)

    with caplog.at_level("DEBUG"):
        response = client.get("/api/status", auth=(FAKE_USERNAME, "wrong-password"))

    assert response.status_code == 401
    assert FAKE_PASSWORD_HASH not in response.text
    assert FAKE_PASSWORD not in response.text
    assert FAKE_PASSWORD_HASH not in caplog.text
    assert FAKE_PASSWORD not in caplog.text


def test_missing_kabom_auth_is_a_clear_error_not_a_silent_bypass(monkeypatch, tmp_path):
    monkeypatch.delenv("KABOM_AUTH", raising=False)
    monkeypatch.setenv("KABOM_DB_PATH", str(tmp_path / "kabom.sqlite3"))
    client = TestClient(app)

    with pytest.raises(ValueError, match="KABOM_AUTH"):
        client.get("/api/status")


# --- KABOM_AUTH=google ---------------------------------------------------------


@pytest.fixture
def fresh_main(monkeypatch):
    """Force a fresh `import kabom.main` so its import-time SessionMiddleware
    wiring reflects THIS test's env vars, not whatever an earlier import (in
    this file or another) left behind. Restores the previously-cached
    module afterwards.

    Also resets kabom.auth's module-level OAuth client registry, since that
    module is never reloaded (only kabom.main is) and otherwise a client
    registered by an earlier test would be reused as-is.
    """
    saved_main = sys.modules.pop("kabom.main", None)
    monkeypatch.setattr(auth, "_oauth", auth.OAuth())

    def _import():
        return importlib.import_module("kabom.main")

    yield _import

    sys.modules.pop("kabom.main", None)
    if saved_main is not None:
        sys.modules["kabom.main"] = saved_main


def _google_env(monkeypatch, tmp_path, *, session_secret=FAKE_SESSION_SECRET):
    monkeypatch.setenv("KABOM_AUTH", "google")
    monkeypatch.setenv("KABOM_GOOGLE_CLIENT_ID", FAKE_GOOGLE_CLIENT_ID)
    monkeypatch.setenv("KABOM_GOOGLE_CLIENT_SECRET", FAKE_GOOGLE_CLIENT_SECRET)
    monkeypatch.setenv("KABOM_ALLOWED_EMAILS", ALLOWED_EMAIL)
    monkeypatch.setenv("KABOM_DB_PATH", str(tmp_path / "kabom.sqlite3"))
    if session_secret is None:
        monkeypatch.delenv("KABOM_SESSION_SECRET", raising=False)
    else:
        monkeypatch.setenv("KABOM_SESSION_SECRET", session_secret)


def test_google_mode_without_session_secret_refuses_to_start(monkeypatch, tmp_path, fresh_main):
    _google_env(monkeypatch, tmp_path, session_secret=None)

    with pytest.raises(ValueError, match="KABOM_SESSION_SECRET"):
        fresh_main()


def test_google_mode_session_secret_error_names_the_variable_not_a_value(
    monkeypatch, tmp_path, fresh_main
):
    """The ticket's "no secret value in an error" bar, checked against the
    exception raised at import time itself."""
    _google_env(monkeypatch, tmp_path, session_secret=None)

    with pytest.raises(ValueError) as exc_info:
        fresh_main()

    assert FAKE_GOOGLE_CLIENT_SECRET not in str(exc_info.value)


def _stub_client(monkeypatch, main_module, token: dict):
    async def fake_authorize_access_token(request, **kwargs):
        return token

    client = main_module.auth.get_google_oauth_client()
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)


def test_google_login_outside_allowlist_is_refused_after_successful_exchange(
    monkeypatch, tmp_path, fresh_main
):
    """The ticket calls this out explicitly: an address outside
    KABOM_ALLOWED_EMAILS must be refused even though the token
    exchange itself looks entirely successful (valid token, verified
    email) — this is the failure that would otherwise go unnoticed."""
    _google_env(monkeypatch, tmp_path)
    main_module = fresh_main()
    _stub_client(
        monkeypatch,
        main_module,
        {"userinfo": {"email": OUTSIDER_EMAIL, "email_verified": True}},
    )
    client = TestClient(main_module.app, base_url="https://testserver")

    callback_response = client.get("/auth/callback", follow_redirects=False)

    assert callback_response.status_code == 403

    # And no session was established: a protected route still 401s.
    protected_response = client.get("/")
    assert protected_response.status_code == 401


def test_google_login_unverified_email_is_refused(monkeypatch, tmp_path, fresh_main):
    _google_env(monkeypatch, tmp_path)
    main_module = fresh_main()
    _stub_client(
        monkeypatch,
        main_module,
        {"userinfo": {"email": ALLOWED_EMAIL, "email_verified": False}},
    )
    client = TestClient(main_module.app, base_url="https://testserver")

    response = client.get("/auth/callback", follow_redirects=False)

    assert response.status_code == 403


def test_google_login_allowed_and_verified_email_is_admitted(monkeypatch, tmp_path, fresh_main):
    _google_env(monkeypatch, tmp_path)
    main_module = fresh_main()
    _stub_client(
        monkeypatch,
        main_module,
        {"userinfo": {"email": ALLOWED_EMAIL, "email_verified": True}},
    )
    conn = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(conn)
    main_module.app.dependency_overrides[main_module.get_db_connection] = lambda: (yield conn)
    try:
        client = TestClient(main_module.app, base_url="https://testserver")

        callback_response = client.get("/auth/callback", follow_redirects=False)
        assert callback_response.status_code in (302, 307)

        # The session cookie set by the callback is enough to reach a
        # protected route with no further credentials.
        status_response = client.get("/api/status")
        assert status_response.status_code == 200
    finally:
        main_module.app.dependency_overrides.pop(main_module.get_db_connection, None)
        conn.close()


def test_google_login_failure_does_not_leak_client_secret_or_session_secret(
    monkeypatch, tmp_path, fresh_main, caplog
):
    _google_env(monkeypatch, tmp_path)
    main_module = fresh_main()

    async def fake_authorize_access_token(request, **kwargs):
        raise main_module.OAuthError(error="access_denied", description="user cancelled")

    oauth_client = main_module.auth.get_google_oauth_client()
    monkeypatch.setattr(oauth_client, "authorize_access_token", fake_authorize_access_token)
    client = TestClient(main_module.app, base_url="https://testserver")

    with caplog.at_level("DEBUG"):
        response = client.get("/auth/callback", follow_redirects=False)

    assert response.status_code == 401
    assert FAKE_GOOGLE_CLIENT_SECRET not in response.text
    assert FAKE_SESSION_SECRET not in response.text
    assert FAKE_GOOGLE_CLIENT_SECRET not in caplog.text
    assert FAKE_SESSION_SECRET not in caplog.text


def test_auth_routes_are_not_found_when_not_in_google_mode(monkeypatch, tmp_path):
    _basic_env(monkeypatch, tmp_path)
    client = TestClient(app)

    assert client.get("/auth/login", follow_redirects=False).status_code == 404
    assert client.get("/auth/callback", follow_redirects=False).status_code == 404


# --- require_auth override, the pattern the other test files rely on --------


def test_require_auth_can_be_overridden_like_get_db_connection(monkeypatch, tmp_path):
    """Documents/protects the pattern tests/test_api.py and tests/test_ui.py
    use to bypass auth entirely rather than weakening production auth."""
    monkeypatch.delenv("KABOM_AUTH", raising=False)
    conn = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(conn)
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[get_db_connection] = lambda: (yield conn)
    try:
        response = TestClient(app).get("/api/status")
        # The point is proven at the auth layer: no ValueError from
        # require_auth despite KABOM_AUTH being unset, because the
        # dependency never ran for real.
        assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(require_auth, None)
        app.dependency_overrides.pop(get_db_connection, None)
        conn.close()
