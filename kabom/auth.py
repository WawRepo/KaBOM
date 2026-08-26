"""Authentication for KaBOM (HOME-233).

Two modes, chosen by `KABOM_AUTH` ("basic" | "google"). There is no "no
auth" mode: this app has no business being reachable, even inside the
homelab, without a check on who is asking. `basic` is meant to be finished,
not a placeholder — it may be the only mode this app ever runs in.

- **basic**: username from `KABOM_BASIC_USER`, password checked against a
  bcrypt hash from `KABOM_BASIC_PASSWORD_HASH` — never a plaintext password
  in an environment variable: it would land in `docker inspect` and in the
  pod spec. Two ways in, both accepted: a person signs in once at `/login`
  and gets a signed session cookie, while an API client sends
  `Authorization: Basic` on every request and never touches the session.
  Nothing ever sends `WWW-Authenticate`, so browsers never raise their
  native credentials popup.
- **google**: authlib's standard OAuth2 authorization-code flow against
  Google, gated by an explicit email allow-list (`KABOM_ALLOWED_EMAILS`),
  with Google's `email_verified` claim checked. The session lives in a
  signed cookie (Starlette's `SessionMiddleware`, backed by itsdangerous),
  which needs `KABOM_SESSION_SECRET`.

Both modes therefore need `KABOM_SESSION_SECRET`, and the app refuses to
start without it rather than generate one: a secret minted at boot
invalidates every session on restart, which gets "fixed" by someone
hardcoding one.

Every function here reads the environment fresh on each call rather than
caching at import time, matching kabom.config's style — env vars are cheap
to read and this keeps `KABOM_AUTH`/credentials changes (e.g. in tests)
honest with no import-order surprises.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from urllib.parse import quote

import bcrypt
from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger(__name__)

# Google's OpenID Connect discovery document. authlib fetches this lazily —
# only on the first actual authorize_redirect/authorize_access_token call —
# never at import time or at client registration, so importing this module
# (and registering the client below) never touches the network.
_GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
_GOOGLE_CLIENT_NAME = "google"

_oauth = OAuth()

_VALID_MODES = ("basic", "google")


def load_auth_mode() -> str:
    """Which auth mode is active. Raises if `KABOM_AUTH` is unset or not one
    of "basic"/"google" — there is no implicit "no auth" fallback."""
    mode = os.environ.get("KABOM_AUTH")
    if mode not in _VALID_MODES:
        raise ValueError(
            f"KABOM_AUTH must be one of {_VALID_MODES!r}, got {mode!r}. "
            "Refusing to guess an auth mode."
        )
    return mode


# --- basic auth --------------------------------------------------------------


@dataclass(frozen=True)
class BasicAuthConfig:
    username: str
    password_hash: bytes  # a bcrypt hash, e.g. b"$2b$12$..."


def load_basic_auth_config() -> BasicAuthConfig:
    """Build a BasicAuthConfig from the environment.

    Raises ValueError naming every missing variable if any are unset, same
    fail-loudly shape as kabom.config.load_s3_config.
    """
    username = os.environ.get("KABOM_BASIC_USER")
    password_hash = os.environ.get("KABOM_BASIC_PASSWORD_HASH")
    missing = [
        name
        for name, value in (
            ("KABOM_BASIC_USER", username),
            ("KABOM_BASIC_PASSWORD_HASH", password_hash),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Missing required basic-auth environment variable(s): " + ", ".join(missing)
        )
    return BasicAuthConfig(username=username, password_hash=password_hash.encode("utf-8"))


def _verify_password(password: str, stored_hash: bytes) -> bool:
    """Constant-time password check against a bcrypt hash.

    The ticket asks for `secrets.compare_digest`, not `==`. bcrypt's own
    `checkpw(password, hash)` API does not hand back two byte strings to
    compare — it returns a bool — so there is nothing to pass to
    `compare_digest` from that call directly. What we do instead is the same
    thing `checkpw` does internally (recompute the hash and compare it to
    the stored one): a bcrypt hash embeds its own salt and cost factor, so
    passing the *stored hash* as the "salt" argument to `bcrypt.hashpw`
    reproduces the identical hash if, and only if, the password matches.
    That gives us two fixed-length byte strings, which we then compare
    explicitly with `secrets.compare_digest` ourselves, rather than relying
    on an internal implementation detail of a third-party library. (For the
    record: the `bcrypt` package's own `checkpw` already does an equivalent
    constant-time comparison via `hmac.compare_digest` — which is the exact
    same function `secrets.compare_digest` re-exports — so this is not
    "fixing" an insecure comparison, it is making the constant-time
    comparison explicit and independent of that implementation detail.)
    """
    try:
        candidate_hash = bcrypt.hashpw(password.encode("utf-8"), stored_hash)
    except ValueError:
        # Malformed stored hash (not a bcrypt hash at all). Never reflect
        # the bad hash value back; treat exactly like a wrong password.
        return False
    return secrets.compare_digest(candidate_hash, stored_hash)


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    """A plain 401, deliberately WITHOUT a `WWW-Authenticate: Basic` header.

    That header is what makes a browser throw up its native credentials
    popup, which is the opposite of what the login page exists for. API
    clients do not need it: curl's `-u` and every HTTP library send Basic
    credentials preemptively rather than waiting to be challenged.
    """
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _redirect_to_login(request: Request) -> HTTPException:
    """Send a browser to the login page instead of 401-ing at it.

    `next` carries where they were headed so login can put them back there,
    and is validated on the way out (see kabom.main.login) — an open
    redirect is exactly the kind of thing a login form invites.
    """
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail="Not authenticated",
        headers={"Location": f"/login?next={quote(target, safe='')}"},
    )


def _wants_html(request: Request) -> bool:
    """Whether to redirect to the login page or answer 401.

    Path-based rather than Accept-header-based: `/api/*` and `/admin/*` are
    for programs and should get a status code they can act on, everything
    else is a page a person is looking at.
    """
    path = request.url.path
    return not (path.startswith("/api") or path.startswith("/admin"))


def check_basic_credentials(username: str, password: str) -> bool:
    """Constant-time check of one username/password pair against the
    configured basic-auth identity. Used by both the login form and the
    `Authorization: Basic` header path."""
    config = load_basic_auth_config()
    # Both checks always run — not `username_ok and then check password` —
    # so a wrong username takes exactly as long as a wrong password. With
    # one user this is not a rich target, but it costs nothing to do right.
    username_ok = secrets.compare_digest(username.encode("utf-8"), config.username.encode("utf-8"))
    password_ok = _verify_password(password, config.password_hash)
    return username_ok and password_ok


def _check_basic_auth(request: Request, credentials: HTTPBasicCredentials | None) -> None:
    """Authenticate a request in `basic` mode, by either route.

    A browser signs in once at /login and gets a signed session cookie; an
    API client sends `Authorization: Basic` on every request and never
    touches the session. Both end up here, and either is sufficient.
    """
    if request.session.get("user"):
        return
    if credentials is not None and check_basic_credentials(
        credentials.username, credentials.password
    ):
        return
    if _wants_html(request):
        raise _redirect_to_login(request)
    raise _unauthorized("Incorrect username or password")


# --- google oauth --------------------------------------------------------


@dataclass(frozen=True)
class GoogleAuthConfig:
    client_id: str
    client_secret: str
    allowed_emails: frozenset[str]
    session_secret: str


def load_session_secret() -> str:
    """`KABOM_SESSION_SECRET`, required in both auth modes.

    Both modes sign a session cookie now: google mode after the OAuth
    callback, basic mode after the login form. Never generated — a secret
    minted at boot logs everyone out on every restart, which is what gets
    "fixed" later by hardcoding one.
    """
    secret = os.environ.get("KABOM_SESSION_SECRET")
    if not secret:
        raise ValueError(
            "KABOM_SESSION_SECRET is not set. Refusing to start rather than "
            "generate one: a generated secret invalidates every session on "
            "restart, which is exactly the kind of thing that gets "
            '"fixed" by someone hardcoding a secret later.'
        )
    return secret


def load_google_auth_config() -> GoogleAuthConfig:
    """Build a GoogleAuthConfig from the environment.

    `KABOM_ALLOWED_EMAILS` is an explicit, comma-separated allow-list — not
    a domain match, not "any Google account". Comparisons are
    case-insensitive since email local-parts/domains are conventionally
    treated that way and Google's own claim is already lower-cased.
    """
    client_id = os.environ.get("KABOM_GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("KABOM_GOOGLE_CLIENT_SECRET")
    allowed_raw = os.environ.get("KABOM_ALLOWED_EMAILS")
    missing = [
        name
        for name, value in (
            ("KABOM_GOOGLE_CLIENT_ID", client_id),
            ("KABOM_GOOGLE_CLIENT_SECRET", client_secret),
            ("KABOM_ALLOWED_EMAILS", allowed_raw),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Missing required Google OAuth environment variable(s): " + ", ".join(missing)
        )

    allowed_emails = frozenset(
        email.strip().lower() for email in allowed_raw.split(",") if email.strip()
    )
    if not allowed_emails:
        raise ValueError("KABOM_ALLOWED_EMAILS is set but contains no addresses")

    # Loaded last and unconditionally: any time a GoogleAuthConfig is built,
    # session-cookie signing is about to be in play, so this is exactly the
    # "at minimum whenever KABOM_AUTH=google" case the ticket calls out.
    session_secret = load_session_secret()

    return GoogleAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        allowed_emails=allowed_emails,
        session_secret=session_secret,
    )


def validate_startup_config() -> None:
    """Validate whatever the active mode needs, once, at app startup.

    Called from kabom.main's lifespan (and, for google mode, effectively
    also at import time via the SessionMiddleware wiring — see
    kabom/main.py). Raising here is "the app refuses to start" — not a
    per-request 500 discovered by whoever happens to hit the API first.
    """
    mode = load_auth_mode()
    # Both modes sign a session cookie, so both need the secret.
    load_session_secret()
    if mode == "basic":
        load_basic_auth_config()
    else:
        load_google_auth_config()


def get_google_oauth_client() -> StarletteOAuth2App:
    """The authlib client for Google, registered once (lazily, on first use)
    from the environment and cached thereafter for the life of the process.

    Registration reads KABOM_GOOGLE_CLIENT_ID/SECRET via
    load_google_auth_config() (raising the same as everywhere else if
    unset) but does not itself contact Google — see _GOOGLE_METADATA_URL's
    comment. Tests never need a real client: kabom/main.py's /auth/callback
    route is exercised by monkeypatching this client's
    `authorize_access_token` coroutine directly, which is authlib's own
    supported per-client API, not a private implementation detail.
    """
    client = _oauth.create_client(_GOOGLE_CLIENT_NAME)
    if client is not None:
        return client

    config = load_google_auth_config()
    _oauth.register(
        name=_GOOGLE_CLIENT_NAME,
        client_id=config.client_id,
        client_secret=config.client_secret,
        server_metadata_url=_GOOGLE_METADATA_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    return _oauth.create_client(_GOOGLE_CLIENT_NAME)


def _check_google_session(request: Request) -> None:
    """Check the signed session cookie against the current allow-list.

    Re-checked on every request, not just at login — if `KABOM_ALLOWED_EMAILS`
    is edited to drop someone, they lose access on their very next request
    rather than staying in until their cookie expires.
    """
    config = load_google_auth_config()
    email = request.session.get("email")
    if not email or email not in config.allowed_emails:
        raise _unauthorized("Not authenticated")


# --- the one dependency every protected route uses --------------------------

_basic_credentials = HTTPBasic(auto_error=False)


async def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic_credentials),
) -> None:
    """The single auth gate. Wired once, as a router-level dependency (see
    kabom/main.py's `protected` router), rather than repeated in every
    handler. `GET /healthz` is the one route registered outside that
    router, so it alone never runs this.
    """
    mode = load_auth_mode()
    if mode == "basic":
        _check_basic_auth(request, credentials)
    else:
        _check_google_session(request)
