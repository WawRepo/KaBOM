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
    # Basic mode signs a login-session cookie too now, so it needs this in
    # exactly the same way google mode does.
    monkeypatch.setenv("KABOM_SESSION_SECRET", FAKE_SESSION_SECRET)
    # Defensive: even though auth should reject these requests before any
    # handler touches the database, point KABOM_DB_PATH at a throwaway file
    # rather than risk a stray kabom.sqlite3 landing in the repo root if
    # dependency-resolution order ever changes.
    monkeypatch.setenv("KABOM_DB_PATH", str(tmp_path / "kabom.sqlite3"))


def _client(main) -> TestClient:
    """A TestClient over https.

    The session cookie is marked Secure (kabom/main.py only relaxes that
    behind KABOM_INSECURE_COOKIES, for local http dev), so a plain
    http://testserver client would silently never send it back and every
    session assertion below would pass for the wrong reason.
    """
    return TestClient(main.app, base_url="https://testserver")


# --- KABOM_AUTH=basic ---------------------------------------------------------
#
# These use the `fresh_main` fixture (defined further down) for the same
# reason the google tests do: kabom/main.py attaches SessionMiddleware once,
# at import time, and basic mode now needs it for the login session.


def test_healthz_needs_no_auth_even_in_basic_mode(monkeypatch, tmp_path, fresh_main):
    _basic_env(monkeypatch, tmp_path)
    main = fresh_main()

    response = _client(main).get("/healthz")

    assert response.status_code == 200


API_ROUTES = [r for r in PROTECTED_ROUTES if r[1].startswith(("/api", "/admin"))]
PAGE_ROUTES = [r for r in PROTECTED_ROUTES if not r[1].startswith(("/api", "/admin"))]


@pytest.mark.parametrize("method,path,kwargs", API_ROUTES)
def test_api_routes_return_401_without_credentials(
    monkeypatch, tmp_path, fresh_main, method, path, kwargs
):
    _basic_env(monkeypatch, tmp_path)
    client = _client(fresh_main())

    response = client.request(method, path, **kwargs)

    assert response.status_code == 401
    # Deliberately no WWW-Authenticate: that header is what makes a browser
    # raise its native credentials popup, which the login page replaces.
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize("method,path,kwargs", PAGE_ROUTES)
def test_page_routes_redirect_to_login_without_credentials(
    monkeypatch, tmp_path, fresh_main, method, path, kwargs
):
    _basic_env(monkeypatch, tmp_path)
    client = _client(fresh_main())

    response = client.request(method, path, follow_redirects=False, **kwargs)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert "www-authenticate" not in response.headers


def test_wrong_password_is_rejected(monkeypatch, tmp_path, fresh_main):
    _basic_env(monkeypatch, tmp_path)
    client = _client(fresh_main())

    response = client.get("/api/status", auth=(FAKE_USERNAME, "not the password"))

    assert response.status_code == 401


def test_wrong_username_is_rejected(monkeypatch, tmp_path, fresh_main):
    _basic_env(monkeypatch, tmp_path)
    client = _client(fresh_main())

    response = client.get("/api/status", auth=("someone-else", FAKE_PASSWORD))

    assert response.status_code == 401


def test_correct_credentials_are_admitted(monkeypatch, tmp_path, fresh_main):
    """An API client sending `Authorization: Basic` never touches the login
    form or the session — the header alone is enough, on every request."""
    _basic_env(monkeypatch, tmp_path)
    main = fresh_main()
    conn = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(conn)
    main.app.dependency_overrides[main.get_db_connection] = lambda: (yield conn)
    try:
        response = _client(main).get("/api/status", auth=(FAKE_USERNAME, FAKE_PASSWORD))
        assert response.status_code == 200
    finally:
        main.app.dependency_overrides.clear()
        conn.close()


# --- the login form ----------------------------------------------------------


def test_login_page_renders_a_form(monkeypatch, tmp_path, fresh_main):
    _basic_env(monkeypatch, tmp_path)

    response = _client(fresh_main()).get("/login")

    assert response.status_code == 200
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text


def test_login_with_correct_credentials_starts_a_session(monkeypatch, tmp_path, fresh_main):
    """The whole point of the form: sign in once, then browse without
    re-sending credentials on every request."""
    _basic_env(monkeypatch, tmp_path)
    main = fresh_main()
    conn = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(conn)
    main.app.dependency_overrides[main.get_db_connection] = lambda: (yield conn)
    try:
        client = _client(main)

        posted = client.post(
            "/login",
            data={"username": FAKE_USERNAME, "password": FAKE_PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert posted.headers["location"] == "/"

        # The TestClient keeps the session cookie, so this carries no
        # credentials of its own.
        assert client.get("/api/status").status_code == 200
    finally:
        main.app.dependency_overrides.clear()
        conn.close()


def test_login_with_wrong_password_does_not_start_a_session(monkeypatch, tmp_path, fresh_main):
    _basic_env(monkeypatch, tmp_path)
    client = _client(fresh_main())

    posted = client.post(
        "/login",
        data={"username": FAKE_USERNAME, "password": "wrong", "next": "/"},
        follow_redirects=False,
    )

    assert posted.status_code == 401
    assert "Incorrect username or password" in posted.text
    # And no session was established by the failed attempt.
    assert client.get("/api/status").status_code == 401


@pytest.mark.parametrize(
    "hostile_next",
    ["https://evil.example/steal", "//evil.example/steal", "javascript:alert(1)"],
)
def test_login_refuses_to_redirect_off_site(monkeypatch, tmp_path, fresh_main, hostile_next):
    """`next` comes straight from the query string, so a login form that
    honours it blindly is an open redirect."""
    _basic_env(monkeypatch, tmp_path)
    client = _client(fresh_main())

    posted = client.post(
        "/login",
        data={"username": FAKE_USERNAME, "password": FAKE_PASSWORD, "next": hostile_next},
        follow_redirects=False,
    )

    assert posted.status_code == 303
    assert posted.headers["location"] == "/"


def test_session_stops_working_when_the_username_is_rotated(monkeypatch, tmp_path, fresh_main):
    """A session cookie is not a bearer token that outlives its credentials.

    Rotate the configured identity after a leak and existing cookies must
    stop working on the very next request, not linger for the cookie's
    lifetime.
    """
    _basic_env(monkeypatch, tmp_path)
    main = fresh_main()
    conn = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(conn)
    main.app.dependency_overrides[main.get_db_connection] = lambda: (yield conn)
    try:
        client = _client(main)
        client.post(
            "/login",
            data={"username": FAKE_USERNAME, "password": FAKE_PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        # The session works...
        assert client.get("/api/status").status_code == 200

        monkeypatch.setenv("KABOM_BASIC_USER", "someone-else-entirely")

        # ...and stops working the moment the identity behind it changes.
        assert client.get("/api/status").status_code == 401
    finally:
        main.app.dependency_overrides.clear()
        conn.close()


def test_htmx_request_gets_hx_redirect_not_a_bare_303(monkeypatch, tmp_path, fresh_main):
    """htmx follows a 303 transparently and swaps the login page into
    whatever it was targeting — on the search box that silently deletes the
    results. HX-Redirect makes it navigate instead."""
    _basic_env(monkeypatch, tmp_path)
    client = _client(fresh_main())

    response = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)

    assert response.status_code == 401
    assert response.headers["hx-redirect"].startswith("/login?next=")
    assert "location" not in response.headers


def test_logout_clears_the_session(monkeypatch, tmp_path, fresh_main):
    _basic_env(monkeypatch, tmp_path)
    client = _client(fresh_main())
    client.post(
        "/login",
        data={"username": FAKE_USERNAME, "password": FAKE_PASSWORD, "next": "/"},
        follow_redirects=False,
    )

    client.post("/logout", follow_redirects=False)

    assert client.get("/api/status").status_code == 401


def test_no_secret_value_appears_in_response_body_or_logs(
    monkeypatch, tmp_path, fresh_main, caplog
):
    _basic_env(monkeypatch, tmp_path)
    client = _client(fresh_main())

    with caplog.at_level("DEBUG"):
        response = client.get("/api/status", auth=(FAKE_USERNAME, "wrong-password"))
        form = client.post(
            "/login",
            data={"username": FAKE_USERNAME, "password": "wrong-password", "next": "/"},
            follow_redirects=False,
        )

    assert response.status_code == 401
    assert form.status_code == 401
    for body in (response.text, form.text, caplog.text):
        assert FAKE_PASSWORD_HASH not in body
        assert FAKE_PASSWORD not in body
        assert FAKE_SESSION_SECRET not in body


# --- behind a TLS-terminating proxy ------------------------------------------


def test_oauth_redirect_uri_uses_the_forwarded_scheme(monkeypatch, tmp_path, fresh_main):
    """The callback URL must be built from the scheme the *client* used.

    TLS terminates at the ingress, so the pod only ever sees plain HTTP.
    Without honouring X-Forwarded-Proto the app hands Google an http://
    redirect_uri and every login dies on `redirect_uri_mismatch` against
    the registered https:// one.
    """
    _google_env(monkeypatch, tmp_path)
    main = fresh_main()

    # TestClient talks straight to the ASGI app, so uvicorn's
    # ProxyHeadersMiddleware is not in the stack. Wrap it the way uvicorn
    # does with FORWARDED_ALLOW_IPS set (see the Dockerfile).
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    client = TestClient(
        ProxyHeadersMiddleware(main.app, trusted_hosts="*"),
        base_url="http://testserver",
    )

    response = client.get(
        "/auth/login",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers["location"]
    assert "redirect_uri=https%3A%2F%2F" in location, (
        f"redirect_uri must use the forwarded https scheme, got: {location}"
    )
    assert "redirect_uri=http%3A%2F%2F" not in location


# --- both sign-in methods at once --------------------------------------------


def test_google_mode_login_page_also_offers_the_password_form(monkeypatch, tmp_path, fresh_main):
    """Google being unreachable must not lock a human out of the UI when a
    password is configured — the two are additive, not alternatives."""
    _google_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KABOM_BASIC_USER", FAKE_USERNAME)
    monkeypatch.setenv("KABOM_BASIC_PASSWORD_HASH", FAKE_PASSWORD_HASH)

    page = _client(fresh_main()).get("/login")

    assert page.status_code == 200
    assert "Sign in with Google" in page.text
    assert 'name="password"' in page.text


def test_google_mode_password_login_works_and_reaches_the_ui(monkeypatch, tmp_path, fresh_main):
    _google_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KABOM_BASIC_USER", FAKE_USERNAME)
    monkeypatch.setenv("KABOM_BASIC_PASSWORD_HASH", FAKE_PASSWORD_HASH)
    main = fresh_main()
    conn = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(conn)
    main.app.dependency_overrides[main.get_db_connection] = lambda: (yield conn)
    try:
        client = _client(main)
        posted = client.post(
            "/login",
            data={"username": FAKE_USERNAME, "password": FAKE_PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        # The session alone gets in — no Authorization header on this request.
        assert client.get("/api/status").status_code == 200
    finally:
        main.app.dependency_overrides.clear()
        conn.close()


def test_google_mode_without_a_password_offers_only_google(monkeypatch, tmp_path, fresh_main):
    """No password configured means no form, and nothing to post to."""
    _google_env(monkeypatch, tmp_path)
    monkeypatch.delenv("KABOM_BASIC_USER", raising=False)
    monkeypatch.delenv("KABOM_BASIC_PASSWORD_HASH", raising=False)
    client = _client(fresh_main())

    page = client.get("/login")
    assert "Sign in with Google" in page.text
    assert 'name="password"' not in page.text

    posted = client.post(
        "/login",
        data={"username": FAKE_USERNAME, "password": FAKE_PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert posted.status_code == 404


def test_basic_mode_login_page_offers_no_google_button(monkeypatch, tmp_path, fresh_main):
    """No OAuth client is configured in basic mode, so offering the button
    would send people to a dead flow."""
    _basic_env(monkeypatch, tmp_path)

    page = _client(fresh_main()).get("/login")

    assert 'name="password"' in page.text
    assert "Sign in with Google" not in page.text


def test_password_session_is_still_refused_for_a_google_allowlist_outsider(
    monkeypatch, tmp_path, fresh_main
):
    """The allow-list must not be weakened by the password path existing:
    a Google session for an unlisted address stays refused."""
    _google_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KABOM_BASIC_USER", FAKE_USERNAME)
    monkeypatch.setenv("KABOM_BASIC_PASSWORD_HASH", FAKE_PASSWORD_HASH)
    main = fresh_main()
    client = _client(main)

    # Forge exactly what a successful-but-unlisted OAuth callback would set.
    with client:
        response = client.get("/api/status")
    assert response.status_code == 401

    monkeypatch.setenv("KABOM_ALLOWED_EMAILS", OUTSIDER_EMAIL + "-different")
    assert client.get("/api/status").status_code == 401


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


# --- google mode: the Basic service account for machines ---------------------


def test_google_mode_accepts_basic_credentials_as_a_service_account(
    monkeypatch, tmp_path, fresh_main
):
    """Cron jobs and monitoring cannot complete an OAuth flow. With the
    basic-auth variables set, google mode still lets them call the API."""
    _google_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KABOM_BASIC_USER", FAKE_USERNAME)
    monkeypatch.setenv("KABOM_BASIC_PASSWORD_HASH", FAKE_PASSWORD_HASH)
    main = fresh_main()
    conn = db.get_connection(str(tmp_path / "kabom.sqlite3"))
    db.init_db(conn)
    main.app.dependency_overrides[main.get_db_connection] = lambda: (yield conn)
    try:
        client = _client(main)
        assert client.get("/api/status", auth=(FAKE_USERNAME, FAKE_PASSWORD)).status_code == 200
        # Wrong password is still refused.
        assert client.get("/api/status", auth=(FAKE_USERNAME, "nope")).status_code == 401
    finally:
        main.app.dependency_overrides.clear()
        conn.close()


def test_google_mode_service_account_is_off_unless_configured(monkeypatch, tmp_path, fresh_main):
    """Without the basic-auth variables, Basic credentials buy nothing —
    Google remains the only way in."""
    _google_env(monkeypatch, tmp_path)
    monkeypatch.delenv("KABOM_BASIC_USER", raising=False)
    monkeypatch.delenv("KABOM_BASIC_PASSWORD_HASH", raising=False)
    client = _client(fresh_main())

    assert client.get("/api/status", auth=(FAKE_USERNAME, FAKE_PASSWORD)).status_code == 401


def test_google_mode_half_configured_service_account_is_a_clear_error(
    monkeypatch, tmp_path, fresh_main
):
    """Setting only one of the pair is a typo, and would silently leave the
    service account switched off."""
    _google_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KABOM_BASIC_USER", FAKE_USERNAME)
    monkeypatch.delenv("KABOM_BASIC_PASSWORD_HASH", raising=False)
    main = fresh_main()

    with pytest.raises(ValueError, match="must be set together"):
        main.auth.validate_startup_config()


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

    # And no session was established. A browser route now lands on the
    # login page rather than a raw JSON 401 — still refused, just with a
    # way back in — while the API keeps answering 401.
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")
    assert client.get("/api/status").status_code == 401


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
