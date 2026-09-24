"""
Admin API: user listing and management, backed directly by Clerk's Backend
API (no local user table - Clerk is the source of truth for accounts).

Every route here depends on require_admin (see app/clerk_auth.py), which
verifies the caller's session token AND checks their role is "admin" via a
live Backend API lookup before any handler runs.
"""
import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import users as user_mirror
from app.clerk_auth import (
    ClerkAPIError,
    ClerkUser,
    clerk_api_request,
    invalidate_role_cache as _invalidate_role_cache,
    require_admin,
)

# Short-lived cache for the users list: the admin page reloads this on every
# mount/refresh, and users rarely change second-to-second, so this turns a
# rapid succession of refreshes into one real Backend API round-trip instead
# of one per refresh. Keyed by (limit, offset, query) so different searches
# don't collide.
_LIST_CACHE_TTL = 15  # seconds
_list_cache: dict[tuple, tuple[float, dict]] = {}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _summarize_user(raw: dict) -> dict:
    """Trim a Clerk User object down to what the admin UI actually renders.
    Clerk's full object includes web3 wallets, external accounts, etc. that
    this app has no use for."""
    emails = raw.get("email_addresses") or []
    primary_email_id = raw.get("primary_email_address_id")
    primary_email = next(
        (e["email_address"] for e in emails if e.get("id") == primary_email_id),
        emails[0]["email_address"] if emails else None,
    )
    return {
        "id": raw.get("id"),
        "email": primary_email,
        "first_name": raw.get("first_name"),
        "last_name": raw.get("last_name"),
        "image_url": raw.get("image_url"),
        "role": (raw.get("public_metadata") or {}).get("role", "user"),
        "banned": raw.get("banned", False),
        "locked": raw.get("locked", False),
        "created_at": raw.get("created_at"),
        "last_active_at": raw.get("last_active_at"),
        "last_sign_in_at": raw.get("last_sign_in_at"),
    }


def _invalidate_list_cache() -> None:
    """Called after any write (role/ban/delete) so the next list load
    reflects it immediately instead of serving a stale cached page."""
    _list_cache.clear()


@router.get("/users")
async def list_users(
    limit: int = 50,
    offset: int = 0,
    query: str | None = None,
    _admin: ClerkUser = Depends(require_admin),
):
    """Active/registered users, newest first. `query` matches Clerk's own
    fuzzy search (name/email/username/phone)."""
    limit = max(1, min(limit, 500))
    cache_key = (limit, offset, query or "")
    cached = _list_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _LIST_CACHE_TTL:
        return cached[1]

    params = {"limit": limit, "offset": offset, "order_by": "-created_at"}
    if query:
        params["query"] = query
    try:
        # Independent requests - run concurrently instead of one after the
        # other, which was adding a full extra round-trip of latency to
        # every page load for no reason.
        raw_users, count = await asyncio.gather(
            clerk_api_request("GET", "/users", params=params),
            clerk_api_request("GET", "/users/count", params={"query": query} if query else None),
        )
    except ClerkAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    result = {
        "users": [_summarize_user(u) for u in raw_users],
        "total": count.get("total_count", len(raw_users)) if isinstance(count, dict) else len(raw_users),
        "limit": limit,
        "offset": offset,
    }
    _list_cache[cache_key] = (time.time(), result)
    return result


class RoleUpdate(BaseModel):
    role: str  # "admin" or "user"


@router.patch("/users/{user_id}/role")
async def set_user_role(user_id: str, body: RoleUpdate, admin: ClerkUser = Depends(require_admin)):
    """Promote/demote a user. Merges into existing public_metadata rather
    than replacing it - Clerk's PATCH overwrites the whole field, so a naive
    {"role": ...} body would silently wipe any other public_metadata keys
    the app (or Clerk dashboard) has set on that user."""
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")
    if user_id == admin.user_id and body.role != "admin":
        raise HTTPException(status_code=400, detail="You cannot demote your own account")

    try:
        current = await clerk_api_request("GET", f"/users/{user_id}")
        merged_metadata = {**(current.get("public_metadata") or {}), "role": body.role}
        # Clerk deprecated public_metadata on PATCH /users/{id} in favor of
        # this dedicated endpoint (confirmed against the live API, which
        # 422s with form_param_deprecated on the old one).
        updated = await clerk_api_request(
            "PATCH",
            f"/users/{user_id}/metadata",
            json={"public_metadata": merged_metadata},
        )
    except ClerkAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    logger.info("Admin %s set role=%s for user %s", admin.user_id, body.role, user_id)
    await user_mirror.upsert_user(updated)
    await user_mirror.record_admin_action(admin.user_id, "set_role", user_id, {"role": body.role})
    _invalidate_role_cache(user_id)
    _invalidate_list_cache()
    return _summarize_user(updated)


@router.post("/users/{user_id}/ban")
async def ban_user(user_id: str, admin: ClerkUser = Depends(require_admin)):
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="You cannot ban your own account")
    try:
        updated = await clerk_api_request("POST", f"/users/{user_id}/ban")
    except ClerkAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    logger.info("Admin %s banned user %s", admin.user_id, user_id)
    await user_mirror.upsert_user(updated)
    await user_mirror.record_admin_action(admin.user_id, "ban", user_id)
    _invalidate_list_cache()
    return _summarize_user(updated)


@router.post("/users/{user_id}/unban")
async def unban_user(user_id: str, admin: ClerkUser = Depends(require_admin)):
    try:
        updated = await clerk_api_request("POST", f"/users/{user_id}/unban")
    except ClerkAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    logger.info("Admin %s unbanned user %s", admin.user_id, user_id)
    await user_mirror.upsert_user(updated)
    await user_mirror.record_admin_action(admin.user_id, "unban", user_id)
    _invalidate_list_cache()
    return _summarize_user(updated)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: ClerkUser = Depends(require_admin)):
    """Permanently deletes the account (Clerk destroys the user record, all
    sessions, all memberships - irreversible). The frontend is responsible
    for confirming with the operator before calling this."""
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    try:
        result = await clerk_api_request("DELETE", f"/users/{user_id}")
    except ClerkAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    logger.info("Admin %s deleted user %s", admin.user_id, user_id)
    await user_mirror.delete_user(user_id)
    await user_mirror.record_admin_action(admin.user_id, "delete", user_id)
    _invalidate_role_cache(user_id)
    _invalidate_list_cache()
    return result


@router.get("/audit-log")
async def audit_log(limit: int = 100, _admin: ClerkUser = Depends(require_admin)):
    """Recent admin actions (role changes, bans, deletes) - Clerk itself has
    no such history, so this is the only record of who did what."""
    entries = await user_mirror.list_audit_log(limit=max(1, min(limit, 500)))
    return {"entries": entries}


@router.get("/me")
async def admin_whoami(admin: ClerkUser = Depends(require_admin)):
    """Lets the frontend confirm admin access (and get a friendly profile)
    without needing a separate non-admin identity endpoint."""
    try:
        profile = await clerk_api_request("GET", f"/users/{admin.user_id}")
    except ClerkAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _summarize_user(profile)
