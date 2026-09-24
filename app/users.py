"""
Local mirror of Clerk users, plus an admin-action audit log.

Clerk remains the source of truth and the only place auth decisions are
verified against (see app/clerk_auth.py's require_admin, which always reads
the LIVE role from Clerk's Backend API, never from this mirror). This
collection exists so the app can query/join user data locally (e.g. a future
"most active users" report) and so app-specific per-user data has something
to key against - without it, every such lookup would mean a Backend API
round-trip.

Kept in sync via the /api/webhooks/clerk endpoint (see app/webhooks.py),
which Clerk calls on user.created/updated/deleted. A user who existed before
that webhook was wired up (or if a delivery is ever missed) is backfilled by
seed_admin_and_backfill() at startup - see app/main.py.
"""
import logging
import time

from app.config import settings
from app.db import get_database

logger = logging.getLogger(__name__)


def _users_collection():
    return get_database()["users"]


def _audit_collection():
    return get_database()["admin_audit_log"]


def _summarize_clerk_user(raw: dict) -> dict:
    """Same trimming as admin_routes._summarize_user, but also keeps
    first_indexed_at intact across upserts - see upsert_user()."""
    emails = raw.get("email_addresses") or []
    primary_email_id = raw.get("primary_email_address_id")
    primary_email = next(
        (e["email_address"] for e in emails if e.get("id") == primary_email_id),
        emails[0]["email_address"] if emails else None,
    )
    return {
        "_id": raw["id"],
        "email": primary_email,
        "first_name": raw.get("first_name"),
        "last_name": raw.get("last_name"),
        "image_url": raw.get("image_url"),
        "role": (raw.get("public_metadata") or {}).get("role", "user"),
        "banned": raw.get("banned", False),
        "locked": raw.get("locked", False),
        "clerk_created_at": raw.get("created_at"),
        "last_active_at": raw.get("last_active_at"),
        "last_sign_in_at": raw.get("last_sign_in_at"),
    }


async def upsert_user(raw_clerk_user: dict) -> None:
    """Write-through from a Clerk User object (webhook payload or Backend
    API response - same JSON shape either way) into the local mirror."""
    doc = _summarize_clerk_user(raw_clerk_user)
    now = time.time()
    doc["synced_at"] = now
    await _users_collection().update_one(
        {"_id": doc["_id"]},
        {"$set": doc, "$setOnInsert": {"first_synced_at": now}},
        upsert=True,
    )


async def delete_user(user_id: str) -> None:
    await _users_collection().delete_one({"_id": user_id})


async def list_users(limit: int = 100) -> list[dict]:
    cursor = _users_collection().find({}).sort("clerk_created_at", -1).limit(limit)
    return [doc async for doc in cursor]


async def record_admin_action(actor_id: str, action: str, target_user_id: str, detail: dict | None = None) -> None:
    """Appends one entry to the admin action history - who did what to
    whom, and when. Never mutated after insert, so this is a true log, not
    just another mutable field on the user doc."""
    await _audit_collection().insert_one(
        {
            "actor_id": actor_id,
            "action": action,
            "target_user_id": target_user_id,
            "detail": detail or {},
            "at": time.time(),
        }
    )


async def list_audit_log(limit: int = 100) -> list[dict]:
    """Recent admin actions, each enriched with the actor's and target's
    display name/email from the local mirror - the raw log only stores
    Clerk user ids (see record_admin_action), which mean nothing on their
    own in a UI. A name lookup miss (e.g. the user was since deleted) falls
    back to showing the bare id rather than failing the whole request.
    """
    cursor = _audit_collection().find({}).sort("at", -1).limit(limit)
    entries = [doc async for doc in cursor]
    if not entries:
        return []

    user_ids = {e["actor_id"] for e in entries} | {e["target_user_id"] for e in entries}
    mirror_docs = _users_collection().find({"_id": {"$in": list(user_ids)}})
    by_id = {doc["_id"]: doc async for doc in mirror_docs}

    def describe(user_id: str) -> dict:
        doc = by_id.get(user_id)
        if not doc:
            return {"id": user_id, "name": None, "email": None}
        name = " ".join(filter(None, [doc.get("first_name"), doc.get("last_name")])) or None
        return {"id": user_id, "name": name, "email": doc.get("email")}

    for entry in entries:
        entry["_id"] = str(entry["_id"])
        entry["actor"] = describe(entry["actor_id"])
        entry["target"] = describe(entry["target_user_id"])
    return entries


async def backfill_from_clerk() -> int:
    """Pulls every Clerk user via the Backend API and upserts them into the
    local mirror. Run once at startup (see app/main.py) to cover accounts
    that existed before the webhook was wired up, or any webhook delivery
    that was missed - the webhook handles new activity from here on, this
    just catches the app up once. Cheap for this app's user counts (a
    handful of accounts), so no incremental/cursor logic - a full re-pull
    every restart is simplest and self-healing against any drift.
    """
    # Imported lazily to avoid a circular import: clerk_auth imports nothing
    # from this module, but both are imported by app.main at startup, and
    # this keeps the dependency direction one-way (users -> clerk_auth only
    # when actually backfilling, not at module load time).
    from app.clerk_auth import ClerkAPIError, clerk_api_request

    if not settings.CLERK_SECRET_KEY or not settings.MONGODB_URI:
        return 0

    try:
        raw_users = await clerk_api_request("GET", "/users", params={"limit": 500})
    except ClerkAPIError:
        logger.exception("Clerk user backfill failed")
        return 0

    for raw in raw_users:
        await upsert_user(raw)
    logger.info("Backfilled %d users from Clerk into local mirror", len(raw_users))
    return len(raw_users)
