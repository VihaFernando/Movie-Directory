"""
Per-user watch history / "Continue Watching", backed by a `watch_history`
Mongo collection.

One entry per TITLE, not per episode: a series watched across several
episodes should show one "Continue Watching" row for the show, always
pointing at the last episode watched, not a separate row per episode. Keyed
by (user_id, source, content_type, slug) - the same shape as app/favorites.py
- with season/episode/progress stored as fields on that one document rather
than in the key, so resuming episode 4 overwrites the row episode 3 left
instead of adding a second one.
"""
import time

from app.db import get_database

# Below this, a title is treated as "not really started" and isn't worth
# resuming - e.g. a few seconds after opening before deciding to back out.
MIN_PROGRESS_SECONDS = 15

# Above this fraction watched, a title counts as finished and is dropped
# from history entirely rather than showing a "Continue Watching" card for
# something there's nothing left to continue.
COMPLETED_FRACTION = 0.95


def _history_collection():
    return get_database()["watch_history"]


def _history_id(user_id: str, source: str, content_type: str, slug: str) -> str:
    return f"{user_id}:{source}:{content_type}:{slug}"


async def ensure_history_indexes() -> None:
    await _history_collection().create_index([("user_id", 1), ("updated_at", -1)])


async def save_progress(user_id: str, item: dict) -> dict | None:
    """Upsert this title's watch progress.

    Returns None (and deletes any existing entry) when the position is
    negligible (see MIN_PROGRESS_SECONDS) or the title is effectively
    finished (see COMPLETED_FRACTION) - both cases mean there is nothing
    meaningful to resume, so keeping a row would show a stale/pointless
    "Continue Watching" card. A season/episode being provided but no
    position is treated as unconditionally worth keeping - the frontend does
    this for a title selected but not yet playing, which existing rows
    should already survive; only remove when a caller can actually judge
    completion (progress_seconds AND duration_seconds present).
    """
    doc_id = _history_id(user_id, item["source"], item["content_type"], item["slug"])
    position = item.get("progress_seconds") or 0
    duration = item.get("duration_seconds") or 0

    if position < MIN_PROGRESS_SECONDS or (duration > 0 and position / duration >= COMPLETED_FRACTION):
        await _history_collection().delete_one({"_id": doc_id, "user_id": user_id})
        return None

    now = time.time()
    await _history_collection().update_one(
        {"_id": doc_id},
        {
            "$set": {
                "user_id": user_id,
                "source": item["source"],
                "content_type": item["content_type"],
                "slug": item["slug"],
                "title": item.get("title", ""),
                "poster_url": item.get("poster_url", ""),
                "backdrop_url": item.get("backdrop_url", ""),
                "year": item.get("year", ""),
                "season": item.get("season"),
                "episode": item.get("episode"),
                "progress_seconds": position,
                "duration_seconds": duration,
                "updated_at": now,
            },
            "$setOnInsert": {"added_at": now},
        },
        upsert=True,
    )
    return {"id": doc_id}


async def remove_history(user_id: str, source: str, content_type: str, slug: str) -> bool:
    doc_id = _history_id(user_id, source, content_type, slug)
    result = await _history_collection().delete_one({"_id": doc_id, "user_id": user_id})
    return result.deleted_count > 0


async def list_history(user_id: str, limit: int = 25) -> list[dict]:
    cursor = (
        _history_collection()
        .find({"user_id": user_id})
        .sort("updated_at", -1)
        .limit(max(1, min(limit, 100)))
    )
    docs = [doc async for doc in cursor]
    for doc in docs:
        doc["id"] = doc.pop("_id")
        doc.pop("user_id", None)
        duration = doc.get("duration_seconds") or 0
        position = doc.get("progress_seconds") or 0
        doc["progress_pct"] = round(min(100, (position / duration) * 100), 1) if duration > 0 else 0
    return docs
