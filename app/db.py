"""
MongoDB connection (Motor async client), shared across the app.

Lazily created on first use and closed on FastAPI shutdown - see
get_database()/close_mongo() and their calls in app/main.py.
"""
import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None


def get_database() -> AsyncIOMotorDatabase:
    """Return the shared database handle, creating the client on first call.

    Raises RuntimeError if MONGODB_URI isn't configured - callers that
    depend on Mongo should let this surface rather than silently no-op.
    """
    global _client
    if not settings.MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not set in the environment/.env")
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGODB_URI)
        logger.info("MongoDB client created for db=%s", settings.MONGODB_DB_NAME)
    return _client[settings.MONGODB_DB_NAME]


async def close_mongo() -> None:
    """Close the shared client so the process doesn't hang open sockets."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def ping() -> bool:
    """Round-trip check used to verify the connection is actually live."""
    db = get_database()
    await db.command("ping")
    return True


# TTL indexes: (collection, datetime field, seconds until expiry). Mongo's
# TTL monitor deletes a document once its field is this many seconds in the
# past - it needs a real BSON date, which is why each collection also
# carries an "expires_at" field alongside the epoch-float timestamp the
# app's own freshness checks use (see catalog_index.py / tmdb.py / main.py).
# Bounding these here means an old cache entry is reclaimed automatically
# rather than growing every one of these collections forever.
_TTL_INDEXES: tuple[tuple[str, str, int], ...] = (
    ("series_details", "expires_at", 0),
    ("tmdb_cache", "expires_at", 0),
    ("subtitle_probes", "expires_at", 0),
)


def expires_at(ttl_seconds: int) -> datetime:
    """A UTC datetime `ttl_seconds` from now, for a document's expires_at
    field - see _TTL_INDEXES."""
    from datetime import timedelta

    return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)


async def ensure_ttl_indexes() -> None:
    """Create each collection's TTL index if it doesn't already exist.

    expireAfterSeconds=0 means "expire exactly at the stored expires_at
    time" - each document sets its own deadline rather than the index
    applying one uniform age, since different collections use different
    TTLs (see the callers of db.expires_at()).
    """
    db = get_database()
    for collection_name, field, seconds in _TTL_INDEXES:
        try:
            await db[collection_name].create_index(field, expireAfterSeconds=seconds)
        except Exception:  # noqa: BLE001 - missing an index degrades to "never expires", not a crash
            logger.warning("Could not create TTL index on %s.%s", collection_name, field, exc_info=True)
