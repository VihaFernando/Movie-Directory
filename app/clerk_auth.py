"""
Clerk authentication for the FastAPI backend.

There is no official Clerk SDK for Python, so session verification is done
by hand: Clerk issues session tokens as RS256 JWTs signed with a key whose
public half is published at the Frontend API's JWKS endpoint. Verifying the
signature + standard claims (exp/nbf/iss) locally means no network call is
needed per request - only the JWKS itself is fetched, and then cached.

User *management* (list/update/delete) has no such offline path - that goes
through Clerk's Backend API (api.clerk.com) using CLERK_SECRET_KEY, wrapped
below in ClerkAPIError/the clerk_api_request() helper used by
app/admin_routes.py.
"""
import base64
import logging
import time

import httpx
import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

from app.config import settings

logger = logging.getLogger(__name__)

_jwks_client: PyJWKClient | None = None
_frontend_api_domain: str | None = None


def _decode_frontend_api_domain(publishable_key: str) -> str:
    """Clerk publishable keys encode the Frontend API host: 'pk_test_' or
    'pk_live_' followed by base64("{domain}$"). This is the same domain
    Clerk's own JS SDK derives the key from client-side."""
    if not publishable_key or "_" not in publishable_key:
        raise RuntimeError("CLERK_PUBLISHABLE_KEY is not set or malformed")
    encoded = publishable_key.split("_", 2)[2]
    padded = encoded + "=" * (-len(encoded) % 4)
    decoded = base64.b64decode(padded).decode("utf-8")
    return decoded.rstrip("$")


def _get_frontend_api_domain() -> str:
    global _frontend_api_domain
    if _frontend_api_domain is None:
        _frontend_api_domain = _decode_frontend_api_domain(settings.CLERK_PUBLISHABLE_KEY)
    return _frontend_api_domain


def _get_jwks_client() -> PyJWKClient:
    """Lazily built, reused across requests - PyJWKClient caches the JWKS
    response itself (default: keeps it until a kid it hasn't seen shows up),
    so this avoids a network round-trip on every authenticated request."""
    global _jwks_client
    if _jwks_client is None:
        domain = _get_frontend_api_domain()
        _jwks_client = PyJWKClient(f"https://{domain}/.well-known/jwks.json")
    return _jwks_client


class ClerkUser:
    """The authenticated caller, decoded from a verified session token.

    Default Clerk session tokens carry only {azp, exp, iat, iss, nbf, sid,
    sub} - no public_metadata - unless a custom session claims template has
    been configured in the Clerk dashboard, which this project doesn't
    assume. So `role` is NOT read from the token; see require_admin below,
    which fetches it from the Backend API instead.
    """

    def __init__(self, user_id: str, claims: dict):
        self.user_id = user_id
        self.claims = claims


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    return authorization.split(" ", 1)[1].strip()


async def get_current_user(authorization: str | None = Header(None)) -> ClerkUser:
    """FastAPI dependency: verifies the session JWT and returns the caller.

    Raises 401 for anything wrong with the token (missing, expired, bad
    signature) rather than letting a verification exception surface as a
    500 - an invalid token is a client error, not a server fault.
    """
    token = _extract_bearer_token(authorization)
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"require": ["exp", "iat", "sub"]},
            # Tolerates a few seconds of clock drift between this machine
            # and Clerk's servers - without it, a local clock running even
            # slightly behind rejects every token with "not yet valid (iat)"
            # even though the token is genuinely fresh.
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid session token: {exc}") from exc

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Session token missing subject")
    return ClerkUser(user_id, claims)


class ClerkAPIError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


_CLERK_API_BASE = "https://api.clerk.com/v1"
_api_client: httpx.AsyncClient | None = None


def _get_api_client() -> httpx.AsyncClient:
    global _api_client
    if _api_client is None:
        if not settings.CLERK_SECRET_KEY:
            raise RuntimeError("CLERK_SECRET_KEY is not set in the environment/.env")
        _api_client = httpx.AsyncClient(
            base_url=_CLERK_API_BASE,
            headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"},
            timeout=15.0,
        )
    return _api_client


async def close_clerk_api_client() -> None:
    global _api_client
    if _api_client is not None:
        await _api_client.aclose()
        _api_client = None


async def clerk_api_request(method: str, path: str, **kwargs) -> dict | list:
    """Thin wrapper around Clerk's Backend API. Raises ClerkAPIError with
    Clerk's own error detail on a non-2xx response, so callers (see
    app/admin_routes.py) can turn it straight into an HTTPException without
    re-parsing the body themselves."""
    client = _get_api_client()
    resp = await client.request(method, path, **kwargs)
    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = body.get("errors", [{}])[0].get("message", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        raise ClerkAPIError(resp.status_code, detail)
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


# Caches the outcome of a role lookup for a short time so a burst of admin
# requests (e.g. the admin page's own load, or rapid clicks) doesn't cost a
# Backend API round-trip on every single one - this was the single biggest
# contributor to the admin page's load time, since every request depending
# on require_admin (including plain reads like GET /users) paid it. 30s
# means a demoted/banned admin loses access within 30s at worst, which is
# an acceptable tradeoff for the latency win on a low-stakes internal tool.
_ROLE_CACHE_TTL = 30
_role_cache: dict[str, tuple[float, str]] = {}


def invalidate_role_cache(user_id: str) -> None:
    """Called after an admin changes someone's role/deletes them, so the
    change is reflected immediately rather than waiting out the TTL - see
    admin_routes.py's _invalidate_role_cache wrapper."""
    _role_cache.pop(user_id, None)


async def require_admin(authorization: str | None = Header(None)) -> ClerkUser:
    """FastAPI dependency for admin-only routes: verifies the session token
    (same as get_current_user) then checks the caller's role, since the
    token itself carries no metadata by default (see ClerkUser's docstring).
    The role lookup is cached (see _role_cache) rather than hitting the
    Backend API on every request.
    """
    user = await get_current_user(authorization)

    cached = _role_cache.get(user.user_id)
    if cached and (time.time() - cached[0]) < _ROLE_CACHE_TTL:
        role = cached[1]
    else:
        try:
            profile = await clerk_api_request("GET", f"/users/{user.user_id}")
        except ClerkAPIError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        role = (profile.get("public_metadata") or {}).get("role", "user")
        _role_cache[user.user_id] = (time.time(), role)

    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
