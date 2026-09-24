"""
Per-user favorites: movies and TV shows a signed-in user has saved.

Keyed by (user_id, source, content_type, slug) so a movie and a TV show that
happen to share a slug never collide, and one user's favorites never leak
into another's queries. Clerk's user id is the only per-user key available -
there is no local users table this could join against for auth purposes
(see app/clerk_auth.py) - so it's stored directly on each favorite document.
"""
import time

from app.db import get_database


def _favorites_collection():
    return get_database()["favorites"]


def _favorite_id(user_id: str, source: str, content_type: str, slug: str) -> str:
    return f"{user_id}:{source}:{content_type}:{slug}"


async def ensure_favorites_indexes() -> None:
    """Index for listing one user's favorites newest-first - the only query
    pattern this collection serves besides the point lookups already covered
    by _id. Safe to call every startup: create_index is a no-op if an
    equivalent index already exists."""
    await _favorites_collection().create_index([("user_id", 1), ("added_at", -1)])


async def add_favorite(user_id: str, item: dict) -> dict:
    """Upsert a favorite for this user. Idempotent: favoriting an already-
    favorited title just refreshes its stored metadata (poster/rating can
    have changed since it was first saved) without changing its original
    added_at, so re-favoriting doesn't bump it to the top of the list.
    """
    doc_id = _favorite_id(user_id, item["source"], item["content_type"], item["slug"])
    now = time.time()
    await _favorites_collection().update_one(
        {"_id": doc_id},
        {
            "$set": {
                "user_id": user_id,
                "source": item["source"],
                "content_type": item["content_type"],
                "slug": item["slug"],
                "title": item.get("title", ""),
                "poster_url": item.get("poster_url", ""),
                "year": item.get("year", ""),
                "rating": item.get("rating"),
            },
            "$setOnInsert": {"added_at": now},
        },
        upsert=True,
    )
    return {"id": doc_id}


async def remove_favorite(user_id: str, source: str, content_type: str, slug: str) -> bool:
    """Returns True if a favorite was actually removed, False if it wasn't
    favorited in the first place - lets the route answer accurately rather
    than claiming success for a no-op."""
    doc_id = _favorite_id(user_id, source, content_type, slug)
    result = await _favorites_collection().delete_one({"_id": doc_id, "user_id": user_id})
    return result.deleted_count > 0


async def list_favorites(user_id: str) -> list[dict]:
    cursor = _favorites_collection().find({"user_id": user_id}).sort("added_at", -1)
    docs = [doc async for doc in cursor]
    for doc in docs:
        doc["id"] = doc.pop("_id")
        doc.pop("user_id", None)
    return docs


async def get_favorite_keys(user_id: str) -> set[str]:
    """Cheap membership set for the frontend to mark hearts as filled across
    a grid, without shipping full favorite documents it won't render."""
    cursor = _favorites_collection().find({"user_id": user_id}, {"source": 1, "content_type": 1, "slug": 1})
    return {f"{doc['source']}:{doc['content_type']}:{doc['slug']}" async for doc in cursor}
