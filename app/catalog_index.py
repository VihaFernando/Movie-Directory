"""
Source catalog index, persisted in MongoDB.

The problem this solves: the UI wants to browse by genre, by rating, by
"trending" - but the source offers none of those. It offers paginated
listings and a search box, nothing more. TMDB *does* offer discovery
endpoints, but those describe TMDB's whole library, most of which is not
playable here. Building categories from them would fill the app with
titles that fail the moment you press Play.

So: page through the source once to learn which titles it actually has,
enrich each with TMDB metadata (the slugs ARE TMDB ids - see app/tmdb.py),
and keep the result in Mongo. Genres, trending and top-rated are then
computed over that set alone, so every tile in the UI is a title the
source can play.

Two kinds of crawl:
  * Full crawl - pages through up to CATALOG_INDEX_MAX_PAGES pages per
    content type, same as before. Used the first time (empty collection)
    and periodically as a safety net (see CATALOG_INDEX_FULL_CRAWL_EVERY_S).
  * Incremental crawl - pages from page 1 and stops as soon as an entire
    page's worth of slugs are already known. These sites list newest first,
    so "everything on this page is already indexed" reliably means
    "everything after this page is too" - letting a refresh find new
    titles in 1-2 page fetches instead of re-walking the whole catalog.

Ranking: each entry gets a `popularity_score` computed from TMDB's own
popularity plus its rating/vote_count (see _popularity_score), and an
`is_recent` flag for anything indexed within CATALOG_INDEX_RECENT_WINDOW_S.
The browse endpoint sorts recent-and-popular first, then by popularity -
see app/main.py.
"""
import asyncio
import logging
import time

from app import tmdb
from app.config import settings
from app.db import get_database
from app.scraper_catalog import scrape_catalog

logger = logging.getLogger(__name__)

_build_task: asyncio.Task | None = None
_state = {"status": "idle", "indexed": 0, "pages": 0, "started": 0.0, "finished": 0.0}

# In-memory mirror of the Mongo collection, refreshed after every crawl and
# on load_from_disk(). The UI's read path (all_entries/browse/genres) stays
# synchronous and fast; Mongo is the source of truth, this is a read cache.
_index: dict[str, dict] = {}


def _key(slug: str, content_type: str, source: str) -> str:
    return f"{source}:{content_type}:{slug}"


def _titles_collection():
    return get_database()["titles"]


def _crawl_state_collection():
    return get_database()["crawl_state"]


def status() -> dict:
    """Current index state, for the UI's loading indicator."""
    return {**_state, "total": len(_index)}


def all_entries(content_type: str | None = None) -> list[dict]:
    """Every indexed title, optionally restricted to one content type."""
    items = list(_index.values())
    if content_type:
        items = [i for i in items if i.get("content_type") == content_type]
    return items


def search_entries(query: str, content_type: str | None = None, source: str | None = None) -> list[dict]:
    """Case-insensitive substring search over already-indexed titles.

    Read path only (in-memory, same data the browse/genre endpoints use) -
    this is what lets /api/catalog answer a search without scraping when the
    title is already known. See app/main.py's get_catalog() for the
    write-through that keeps this collection growing on a miss.
    """
    needle = query.strip().lower()
    if not needle:
        return []
    items = all_entries(content_type)
    if source:
        items = [i for i in items if i.get("source") == source]
    return [i for i in items if needle in i.get("title", "").lower()]


async def index_titles(entries_with_source: list[tuple[dict, str]]) -> None:
    """Enrich and upsert ad-hoc titles (e.g. a live search hit) into Mongo.

    Shares _enrich/_upsert_entries with the crawler so a search-triggered
    save gets identical treatment (TMDB enrichment, popularity_score,
    first_indexed_at) to one found by the periodic crawl - the collection
    should not be able to tell which path a title arrived through.
    """
    by_source: dict[str, list[dict]] = {}
    for movie, source in entries_with_source:
        key = _key(movie.detail_page_slug, movie.content_type, source)
        if key in _index:
            continue
        by_source.setdefault(source, []).append(movie)

    for source, movies in by_source.items():
        entries = await asyncio.gather(
            *(
                _enrich(m.detail_page_slug, m.content_type, m.title, m.thumbnail_url)
                for m in movies
            )
        )
        await _upsert_entries(entries, source)


def _popularity_score(tmdb_data: dict | None, rating: float | None) -> float:
    """Blend TMDB's popularity with its rating so a well-reviewed title
    with modest raw popularity still ranks respectably.

    TMDB's raw `popularity` is unbounded and dominated by whatever is
    trending globally *this week*, which would bury a solid but quieter
    title. Rating alone (see the old sort) has the opposite problem: a
    5-vote 9.0 outranks a 50,000-vote 8.0. Multiplying popularity by a
    rating-derived factor keeps popularity as the main signal while letting
    genuinely well-rated titles push above near-identical popularity peers.
    """
    popularity = 0.0
    if tmdb_data:
        try:
            popularity = float(tmdb_data.get("popularity") or 0.0)
        except (TypeError, ValueError):
            popularity = 0.0
    rating_factor = (rating / 10.0) if rating else 0.5
    return round(popularity * (0.5 + rating_factor), 3)


async def load_from_disk() -> None:
    """Load the index from MongoDB into the in-memory read cache.

    Named load_from_disk() for parity with the startup hook that calls it;
    "disk" here means "the titles collection", not a JSON file.
    """
    try:
        cursor = _titles_collection().find({})
        entries = {}
        async for doc in cursor:
            doc.pop("_id", None)
            key = _key(doc["slug"], doc["content_type"], doc["source"])
            entries[key] = doc
        _index.clear()
        _index.update(entries)
        _state["indexed"] = len(_index)
        state_doc = await _crawl_state_collection().find_one({"_id": "global"})
        if state_doc:
            _state["finished"] = state_doc.get("last_full_crawl_at", 0.0)
        logger.info("Loaded %d indexed titles from MongoDB", len(_index))
    except Exception:  # noqa: BLE001 - a Mongo hiccup must not stop startup
        logger.warning("Could not load catalog index from MongoDB; starting empty", exc_info=True)


async def _enrich(slug: str, content_type: str, fallback_title: str, thumbnail: str) -> dict:
    """Build one index entry, using TMDB metadata where available.

    Falls back to what the catalog scrape itself reported, so a title TMDB
    has never heard of still appears and stays playable - just with thinner
    metadata than its neighbours.
    """
    now = time.time()
    entry = {
        "slug": slug,
        "content_type": content_type,
        "title": fallback_title,
        "poster_url": thumbnail,
        "backdrop_url": "",
        "overview": "",
        "rating": None,
        "year": "",
        "genres": [],
        "genre_ids": [],
        "popularity_score": 0.0,
        "last_enriched_at": now,
    }

    data = await tmdb.get_title(slug, content_type)
    if data:
        meta = tmdb.normalise_title(data, content_type)
        # Keep the scraped values wherever TMDB has nothing better, rather
        # than blanking a field that already had something in it.
        for field, value in meta.items():
            if value not in ("", None, []):
                entry[field] = value
        if not entry["poster_url"]:
            entry["poster_url"] = thumbnail
    entry["popularity_score"] = _popularity_score(data, entry["rating"])
    return entry


async def _upsert_entries(entries: list[dict], source: str) -> None:
    """Write freshly scraped/enriched entries to Mongo, preserving
    first_indexed_at for titles that already existed."""
    if not entries:
        return
    collection = _titles_collection()
    now = time.time()
    for entry in entries:
        entry["source"] = source
        entry["last_scraped_at"] = now
        doc_id = _key(entry["slug"], entry["content_type"], source)
        existing = _index.get(doc_id)
        entry["first_indexed_at"] = existing["first_indexed_at"] if existing else now
        await collection.update_one(
            {"_id": doc_id},
            {"$set": entry, "$setOnInsert": {"_id": doc_id}},
            upsert=True,
        )
        _index[doc_id] = {**entry, "_id": doc_id}
    _state["indexed"] = len(_index)


async def index_page(source: str, content_type: str, page: int) -> tuple[int, int]:
    """Index one catalog page.

    Returns (titles_on_page, new_titles_on_page) - the second is what the
    incremental crawl uses to decide whether it has caught up to already-
    known titles.
    """
    movies = await scrape_catalog(source, content_type, "", page)
    if not movies:
        return 0, 0

    new_count = sum(
        1 for m in movies if _key(m.detail_page_slug, content_type, source) not in _index
    )

    # Enrich concurrently - these are independent TMDB reads, and doing them
    # one at a time would make indexing far slower than the scrape itself.
    entries = await asyncio.gather(
        *(
            _enrich(m.detail_page_slug, content_type, m.title, m.thumbnail_url)
            for m in movies
        )
    )
    await _upsert_entries(entries, source)
    return len(movies), new_count


async def _crawl(source: str, max_pages: int, stop_on_no_new: bool) -> None:
    """Shared paging loop for both full and incremental crawls.

    stop_on_no_new=True is the incremental mode: since listings are newest
    first, a page containing zero titles we hadn't already indexed means
    every later page is also already known, so crawling can stop there
    rather than re-walking the full catalog.
    """
    batch_size = max(1, settings.CATALOG_INDEX_CONCURRENCY)

    for content_type in ("movie", "tv"):
        exhausted = False
        page = 1
        while page <= max_pages and not exhausted:
            pages = list(range(page, min(page + batch_size, max_pages + 1)))
            results = await asyncio.gather(
                *(index_page(source, content_type, p) for p in pages),
                return_exceptions=True,
            )

            for requested, result in zip(pages, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "Indexing failed for %s page %d: %s", content_type, requested, result
                    )
                    continue
                total, new = result
                _state["pages"] += 1
                if total < settings.CATALOG_PAGE_SIZE:
                    exhausted = True
                elif stop_on_no_new and new == 0:
                    exhausted = True

            page += batch_size
            if not exhausted:
                # Breathe between batches so the crawl does not hammer the
                # source, which is what gets a scraper blocked.
                await asyncio.sleep(settings.CATALOG_INDEX_DELAY_S)


async def build_index(source: str = "vidbox", max_pages: int | None = None) -> None:
    """Full crawl: page through up to max_pages pages per content type and
    index everything found. Runs in the background; the app serves whatever
    is indexed so far."""
    max_pages = max_pages or settings.CATALOG_INDEX_MAX_PAGES
    _state.update({"status": "building", "started": time.time(), "pages": 0})
    logger.info("Building catalog index (max %d pages per type)", max_pages)

    try:
        await _crawl(source, max_pages, stop_on_no_new=False)
        now = time.time()
        _state.update({"status": "ready", "finished": now})
        await _crawl_state_collection().update_one(
            {"_id": "global"},
            {"$set": {"last_full_crawl_at": now}},
            upsert=True,
        )
        logger.info("Catalog index built: %d titles", len(_index))
    except asyncio.CancelledError:
        _state["status"] = "cancelled"
        raise
    except Exception:  # noqa: BLE001
        _state["status"] = "error"
        logger.exception("Catalog index build failed")


async def refresh_index(source: str = "vidbox") -> None:
    """Incremental crawl: only fetch pages until every title on a page is
    already indexed. Much cheaper than build_index() and meant to run
    routinely (see app/main.py's background scheduler)."""
    _state.update({"status": "refreshing", "started": time.time(), "pages": 0})
    logger.info("Refreshing catalog index (incremental)")

    try:
        await _crawl(source, settings.CATALOG_INDEX_MAX_PAGES, stop_on_no_new=True)
        now = time.time()
        _state.update({"status": "ready", "finished": now})
        await _crawl_state_collection().update_one(
            {"_id": "global"},
            {"$set": {"last_incremental_crawl_at": now}},
            upsert=True,
        )
        logger.info("Catalog index refreshed: %d titles total", len(_index))
    except asyncio.CancelledError:
        _state["status"] = "cancelled"
        raise
    except Exception:  # noqa: BLE001
        _state["status"] = "error"
        logger.exception("Catalog index refresh failed")


def ensure_building(source: str = "vidbox") -> None:
    """Start a background index build if one is not already running.

    Called on startup and whenever a category page finds the index empty,
    so the crawl begins without the user having to trigger it explicitly.
    Chooses a full crawl only when the index is empty or stale beyond
    CATALOG_INDEX_TTL; otherwise this is a no-op (the periodic scheduler in
    app/main.py handles incremental refreshes instead).
    """
    global _build_task
    if _build_task is not None and not _build_task.done():
        return
    age = time.time() - (_state.get("finished") or 0)
    if _index and age < settings.CATALOG_INDEX_TTL:
        _state["status"] = "ready"
        return
    _build_task = asyncio.ensure_future(build_index(source))


def ensure_refreshing(source: str = "vidbox") -> None:
    """Start a background incremental refresh if nothing is already running."""
    global _build_task
    if _build_task is not None and not _build_task.done():
        return
    _build_task = asyncio.ensure_future(refresh_index(source))


async def stop() -> None:
    """Cancel any in-flight build. Called on shutdown."""
    if _build_task is not None and not _build_task.done():
        _build_task.cancel()
        try:
            await _build_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
