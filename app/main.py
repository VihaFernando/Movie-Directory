"""
FastAPI application entrypoint.

Endpoints:
    GET /api/catalog              -> scrape + return movie list (cached)
    GET /api/get_embed/{slug}     -> resolve a movie's embed/stream URL
    GET /api/proxy_embed?url=...  -> proxy+sanitize an embed URL for iframe use

Run with (see run.py docstring for why this matters on Windows):
    python run.py

Plain `uvicorn app.main:app --reload` also works on macOS/Linux, but on
Windows uvicorn's --reload mode force-sets WindowsSelectorEventLoopPolicy,
which cannot spawn subprocesses - breaking Playwright (Chromium is launched
as a subprocess). run.py sets WindowsProactorEventLoopPolicy and runs
without --reload to avoid that.
"""
import asyncio
import logging
import sys
import time
from urllib.parse import quote, unquote, urlparse

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app import catalog_index, favorites, tmdb, users as user_mirror, watch_history
from app.admin_routes import router as admin_router
from app.browser_pool import close_browser, warm_up
from app.camoufox_pool import close_camoufox
from app.clerk_auth import ClerkUser, close_clerk_api_client, get_current_user
from app.config import settings
from app.db import close_mongo, ping as mongo_ping
from app.webhooks import router as webhooks_router
from app.models import CatalogResponse, EmbedResponse, FavoriteIn, Movie, TitleDetail, WatchProgressIn
from app.proxy import (
    build_proxy_response_headers,
    close_image_client,
    fetch_hls_manifest,
    fetch_image,
    fetch_player_asset,
    fetch_sanitized_embed,
    fetch_subtitle,
    is_allowed_embed_url,
    is_allowed_image_url,
    is_direct_media_url,
    is_hls_manifest_url,
    StreamAuthError,
    stream_media,
)
from app.scraper_catalog import scrape_catalog
from app.scraper_series import scrape_title_detail
from app.scraper_embed import capture_embed_url

logging.basicConfig(level=logging.INFO)
# httpx/httpcore log every outgoing request at INFO (one line per TMDB call,
# one per proxied image) - with 300+ indexed titles and posters on every
# browse page, this drowns out the app's own logs. WARNING still surfaces
# real problems (connection errors, retries) from both.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = FastAPI(title="Local Movie Directory")

# Local-only tool: permissive CORS is fine since it never leaves localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router)
app.include_router(webhooks_router)


@app.on_event("startup")
async def _startup() -> None:
    """Restore the catalog index and start refreshing it in the background.

    The crawl is deliberately not awaited: the app serves whatever is
    already indexed while later pages are still being fetched, so a cold
    start is usable immediately rather than blocking on a full crawl.
    """
    if settings.MONGODB_URI:
        try:
            await mongo_ping()
            logger.info("MongoDB connection verified")
            from app.db import ensure_ttl_indexes

            await ensure_ttl_indexes()
            await favorites.ensure_favorites_indexes()
            await watch_history.ensure_history_indexes()
        except Exception:  # noqa: BLE001 - don't block startup on Mongo being down
            logger.exception("MongoDB ping failed")
        try:
            await user_mirror.backfill_from_clerk()
        except Exception:  # noqa: BLE001 - the mirror is a convenience, not load-bearing for auth
            logger.exception("Clerk user backfill failed")
    await catalog_index.load_from_disk()
    # Launch the browser pool now rather than on someone's first click: a
    # cold Chromium launch costs several seconds, and paying it at boot is
    # free. Not awaited-on-failure - warm_up() swallows its own errors and
    # the pool retries on demand.
    await warm_up()
    if settings.CATALOG_INDEX_ON_STARTUP:
        catalog_index.ensure_building()
    _start_refresh_scheduler()


_refresh_scheduler_task: asyncio.Task | None = None


def _start_refresh_scheduler() -> None:
    """Launch the background loop that periodically runs an incremental
    catalog refresh (see catalog_index.refresh_index).

    A no-op if MongoDB isn't configured - there's nowhere to persist the
    refreshed index in that case, so the app just keeps behaving as it did
    before Mongo existed (a one-off crawl on startup, in-memory only).
    """
    global _refresh_scheduler_task
    if not settings.MONGODB_URI:
        return
    if _refresh_scheduler_task is not None and not _refresh_scheduler_task.done():
        return

    async def _loop() -> None:
        while True:
            await asyncio.sleep(settings.CATALOG_INDEX_REFRESH_INTERVAL_S)
            try:
                catalog_index.ensure_refreshing()
            except Exception:  # noqa: BLE001 - a missed refresh should not kill the loop
                logger.exception("Scheduled catalog refresh failed to start")

    _refresh_scheduler_task = asyncio.ensure_future(_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Close the shared browser instance so Chromium doesn't linger after
    the server exits."""
    global _refresh_scheduler_task
    if _refresh_scheduler_task is not None and not _refresh_scheduler_task.done():
        _refresh_scheduler_task.cancel()
        try:
            await _refresh_scheduler_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    await catalog_index.stop()
    await tmdb.close()
    await close_browser()
    await close_camoufox()
    await close_image_client()
    await close_clerk_api_client()
    await close_mongo()


# --- Simple in-memory cache to avoid re-scraping on every page load ---
_catalog_cache: dict[str, tuple[float, list]] = {}

# Season/episode structure per (source, slug, season). In-process cache
# layer in front of the series_details Mongo collection (see
# _series_details_collection / _load_series_detail / _save_series_detail
# below) - this dict just saves a Mongo round-trip within the same process
# for the same TTL window; Mongo is what makes the result survive a
# restart, and what the biggest cost here (driving the watch page's Play
# button and season picker, ~10s+) is worth persisting past.
_series_cache: dict[str, tuple[float, TitleDetail]] = {}


def _series_details_collection():
    from app.db import get_database

    return get_database()["series_details"]


def _series_detail_key(source: str, content_type: str, slug: str, season: int | None) -> str:
    return f"{source}:{content_type}:{slug}:{season if season is not None else 'default'}"


async def _load_series_detail(cache_key: str) -> TitleDetail | None:
    """Return a still-fresh persisted detail for `cache_key`, or None."""
    if not settings.MONGODB_URI:
        return None
    try:
        doc = await _series_details_collection().find_one({"_id": cache_key})
    except Exception:  # noqa: BLE001 - Mongo being unavailable must not break detail lookups
        logger.warning("series_details lookup failed for %s", cache_key, exc_info=True)
        return None
    if not doc:
        return None
    if (time.time() - doc.get("saved_at", 0)) >= settings.SERIES_DETAIL_CACHE_TTL:
        return None
    doc.pop("_id", None)
    doc.pop("saved_at", None)
    return TitleDetail(**doc)


async def _save_series_detail(cache_key: str, detail: TitleDetail) -> None:
    """Persist `detail` so a restart or another worker doesn't have to
    re-drive the watch page for the same (source, slug, season)."""
    if not settings.MONGODB_URI:
        return
    from app.db import expires_at

    try:
        payload = detail.model_dump()
        payload["saved_at"] = time.time()
        payload["expires_at"] = expires_at(settings.SERIES_DETAIL_CACHE_TTL)
        await _series_details_collection().update_one(
            {"_id": cache_key}, {"$set": payload}, upsert=True
        )
    except Exception:  # noqa: BLE001 - persistence is an optimisation only
        logger.warning("series_details save failed for %s", cache_key, exc_info=True)


def _subtitle_probes_collection():
    from app.db import get_database

    return get_database()["subtitle_probes"]


async def _load_subtitle_probe(cache_key: str) -> bool | None:
    """Return a previously probed availability for `cache_key`, or None if
    it has never been probed (or Mongo is unavailable) - never expires,
    since a title's subtitle availability on the CDN doesn't change once
    set."""
    if not settings.MONGODB_URI:
        return None
    try:
        doc = await _subtitle_probes_collection().find_one({"_id": cache_key})
    except Exception:  # noqa: BLE001 - Mongo being unavailable must not break subtitles
        logger.warning("subtitle_probes lookup failed for %s", cache_key, exc_info=True)
        return None
    return doc.get("available") if doc else None


async def _save_subtitle_probe(cache_key: str, available: bool) -> None:
    if not settings.MONGODB_URI:
        return
    from app.db import expires_at

    try:
        await _subtitle_probes_collection().update_one(
            {"_id": cache_key},
            {
                "$set": {
                    "available": available,
                    "probed_at": time.time(),
                    "expires_at": expires_at(settings.SUBTITLE_PROBE_TTL),
                }
            },
            upsert=True,
        )
    except Exception:  # noqa: BLE001 - persistence is an optimisation only
        logger.warning("subtitle_probes save failed for %s", cache_key, exc_info=True)

# --- Embed-resolution cache/dedup ---
# Keyed by slug. Serves two purposes:
#  1. Coalescing: the frontend fires a prefetch on hover and a real request
#     on click - without this, both would independently launch a browser
#     capture for the same slug. Storing the in-flight asyncio.Task lets a
#     second caller await the first one's result instead of starting a
#     duplicate capture.
#  2. TTL cache: a click shortly after a hover-prefetch returns instantly
#     instead of re-resolving, and - now that the frontend can restore
#     playback after a refresh (see frontend/src/hooks/useUrlSync.js) - a
#     refresh minutes into watching something reuses the same capture
#     instead of paying for a fresh ~10-45s Playwright scrape.
#
#     Kept well short of the captured token's own lifetime (observed ~4h
#     JWT - see proxy.py's StreamAuthError docstring) rather than anywhere
#     close to it: this is a safety margin, not a target. A URL that goes
#     stale before this TTL elapses is not a hazard either way - the
#     frontend already recovers from that on its own (see usePlayer.js's
#     HLS error handler, which re-resolves on a 410/fatal network error up
#     to MAX_STALE_RETRIES times), independent of this cache entirely.
_embed_cache: dict[str, tuple[float, asyncio.Task]] = {}
EMBED_CACHE_TTL = 900  # seconds (15 min)


def _invalidate_cached_embed(rejected_url: str) -> None:
    """Drop any cached capture invalidated by the CDN rejecting
    `rejected_url`'s token.

    Called when the CDN refuses a URL for auth reasons: the cached entry
    that produced it is known dead, so keeping it would make every retry
    within EMBED_CACHE_TTL hand back the same dead link.

    Matching is by host, not by exact URL. The rejection usually arrives on
    a variant playlist or a segment - URLs rewritten out of the master and
    therefore never equal to the captured master URL - so an equality check
    would fail to evict anything in exactly the case that matters. Every
    URL in one playback session comes from the same rotating host, and each
    title gets its own, so the host identifies the dead session precisely.
    """
    rejected_host = (urlparse(rejected_url).hostname or "").lower()
    if not rejected_host:
        return
    for key, (_, task) in list(_embed_cache.items()):
        if task.done() and not task.cancelled() and task.exception() is None:
            cached_host = (urlparse(task.result() or "").hostname or "").lower()
            if cached_host and cached_host == rejected_host:
                _embed_cache.pop(key, None)
                logger.info("Invalidated cached embed for %s (stale token)", key)


def _embed_cache_key(slug: str, source: str, content_type: str, season, episode) -> str:
    """Cache key for one resolvable stream.

    Season/episode are part of the key because for a series they identify
    DIFFERENT streams - keying on the slug alone (as this used to) made
    every episode of a show collide on one entry, so opening episode 2
    within the TTL handed back episode 1's URL.
    """
    if content_type == "tv":
        return f"{source}:{content_type}:{slug}:s{season or 1}e{episode or 1}"
    return f"{source}:{content_type}:{slug}"


def _get_or_start_embed_capture(
    slug: str, source: str, content_type: str, season: int | None = None, episode: int | None = None
) -> asyncio.Task:
    """Return the in-flight or cached capture_embed_url() task for this
    slug/episode, starting a new one if none is fresh."""
    now = time.time()
    cache_key = _embed_cache_key(slug, source, content_type, season, episode)
    cached = _embed_cache.get(cache_key)
    if cached and (now - cached[0]) < EMBED_CACHE_TTL:
        return cached[1]

    task = asyncio.ensure_future(capture_embed_url(slug, source, content_type, season, episode))
    # A prefetch-only caller (see /api/prefetch_embed) never awaits this
    # task directly - without a handler, a failed prefetch would log an
    # "exception was never retrieved" warning. get_embed() still sees and
    # raises the real error when it awaits the same task itself.
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    _embed_cache[cache_key] = (now, task)
    return task


@app.get("/api/catalog", response_model=CatalogResponse)
async def get_catalog(
    source: str = Query("vidbox", pattern="^(current|bingeflix|vidbox)$"),
    content_type: str = Query("movie", pattern="^(movie|tv)$"),
    query: str = Query(""),
    page: int = Query(1, ge=1, le=500),
    force_refresh: bool = False,
):
    """Return the scraped movie catalog, using a short-lived cache so
    repeated frontend loads don't re-launch a browser every time.

    For a search (non-empty `query`) on page 1, the already-indexed titles
    in MongoDB are checked first - most searches are for something the
    catalog crawl already found, and answering from there costs no browser
    render at all. Only a miss falls through to a live scrape, and whatever
    that scrape finds is saved to Mongo (see catalog_index.index_titles) so
    the same search is a DB hit next time. Page 1 only: Mongo search has no
    pagination story of its own yet, and a query rarely needs page 2+ once
    it can search a title's full name rather than the source's own
    prefix-match search.
    """
    now = time.time()
    stripped_query = query.strip()
    cache_key = f"{source}:{content_type}:{stripped_query.lower()}:{page}"
    cached = _catalog_cache.get(cache_key)

    if not force_refresh and cached and (now - cached[0]) < settings.CATALOG_CACHE_TTL:
        movies = cached[1]
    elif not force_refresh and stripped_query and page == 1:
        indexed_hits = catalog_index.search_entries(stripped_query, content_type, source)
        if indexed_hits:
            movies = [
                Movie(
                    title=hit["title"],
                    thumbnail_url=hit.get("poster_url", ""),
                    detail_page_slug=hit["slug"],
                    source=hit["source"],
                    content_type=hit["content_type"],
                )
                for hit in indexed_hits
            ]
            _catalog_cache[cache_key] = (now, movies)
        else:
            try:
                movies = await scrape_catalog(source, content_type, stripped_query, page)
            except Exception as exc:  # noqa: BLE001 - surface as HTTP error, log detail
                logger.exception("Catalog scrape failed")
                raise HTTPException(status_code=502, detail=f"Catalog scrape failed: {exc}") from exc
            _catalog_cache[cache_key] = (now, movies)
            if settings.MONGODB_URI and movies:
                asyncio.ensure_future(
                    catalog_index.index_titles([(m, source) for m in movies])
                )
    else:
        try:
            movies = await scrape_catalog(source, content_type, stripped_query, page)
        except Exception as exc:  # noqa: BLE001 - surface as HTTP error, log detail
            logger.exception("Catalog scrape failed")
            raise HTTPException(status_code=502, detail=f"Catalog scrape failed: {exc}") from exc
        _catalog_cache[cache_key] = (now, movies)

    return CatalogResponse(count=len(movies), movies=movies, source=source, content_type=content_type)


@app.get("/api/index_status")
async def get_index_status():
    """Progress of the source catalog crawl.

    The UI shows this while the index is still filling, so a cold start
    reads as "still loading" rather than "this category is empty".
    """
    return catalog_index.status()


@app.get("/api/browse")
async def browse(
    response: Response,
    content_type: str = Query("movie", pattern="^(movie|tv|all)$"),
    genre: str = Query("", description="Genre name to filter by, e.g. 'Drama'"),
    sort: str = Query("popular", pattern="^(popular|top_rated|newest|trending)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=60),
    source: str = Query("vidbox", pattern="^(current|bingeflix|vidbox)$"),
):
    """Browse the indexed catalog by genre and ordering.

    Everything returned here comes from titles the source actually has -
    the index is built by crawling its own listings (see
    app/catalog_index.py). That is the whole point: TMDB's discovery
    endpoints would return a far larger catalog, most of which could not
    be played.
    """
    catalog_index.ensure_building(source)

    items = catalog_index.all_entries(None if content_type == "all" else content_type)

    if genre:
        wanted = genre.strip().lower()
        items = [i for i in items if any(g.lower() == wanted for g in i.get("genres", []))]

    if sort == "top_rated":
        items = sorted(items, key=lambda i: (i.get("rating") or 0), reverse=True)
    elif sort == "newest":
        items = sorted(items, key=lambda i: (i.get("year") or ""), reverse=True)
    else:
        # "popular" and "trending" (the default): titles indexed into our DB
        # within CATALOG_INDEX_RECENT_WINDOW_S surface first (a newly added
        # title should be visible, not buried under a long tail of older
        # popular titles), ranked among themselves by popularity_score;
        # everything else follows, also by popularity_score. See
        # catalog_index._popularity_score for how that score is built.
        cutoff = time.time() - settings.CATALOG_INDEX_RECENT_WINDOW_S
        items = sorted(
            items,
            key=lambda i: (
                (i.get("first_indexed_at") or 0) >= cutoff,
                i.get("popularity_score") or 0,
            ),
            reverse=True,
        )

    total = len(items)
    start = (page - 1) * page_size
    window = items[start : start + page_size]
    # A short browser cache: navigating back to a category the user just
    # left should not re-request an identical list. Kept brief so the page
    # still reflects the crawl as it fills in.
    response.headers["Cache-Control"] = "private, max-age=60"
    return {
        "items": window,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": start + page_size < total,
        "index": catalog_index.status(),
    }


@app.get("/api/genres")
async def get_genres(
    response: Response,
    content_type: str = Query("movie", pattern="^(movie|tv|all)$"),
    source: str = Query("vidbox", pattern="^(current|bingeflix|vidbox)$"),
):
    """Genres actually present in the indexed catalog, with title counts.

    Derived from the index rather than from TMDB's full genre list, so the
    UI never offers a category that would turn out to be empty here.
    """
    catalog_index.ensure_building(source)
    items = catalog_index.all_entries(None if content_type == "all" else content_type)

    counts: dict[str, int] = {}
    for item in items:
        for genre in item.get("genres", []):
            counts[genre] = counts.get(genre, 0) + 1

    genres = [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    response.headers["Cache-Control"] = "private, max-age=60"
    return {"genres": genres, "index": catalog_index.status()}


@app.get("/api/detail/{slug}", response_model=TitleDetail)
async def get_detail(
    slug: str,
    source: str = Query("vidbox", pattern="^(current|bingeflix|vidbox)$"),
    content_type: str = Query("tv", pattern="^(movie|tv)$"),
    season: int | None = Query(None, ge=0, description="Season whose episodes to include"),
    force_refresh: bool = False,
):
    """Return everything the detail page shows for one title.

    For a series, episodes come back for a single season at a time: the
    source only ever renders one season's list at a time, so fetching them
    all would mean driving its season picker once per season. The frontend
    asks again when the user switches season, and each (slug, season) pair
    is cached separately.
    """
    now = time.time()
    cache_key = _series_detail_key(source, content_type, slug, season)
    cached = _series_cache.get(cache_key)

    if not force_refresh and cached and (now - cached[0]) < settings.CATALOG_CACHE_TTL:
        return cached[1]

    if not force_refresh:
        persisted = await _load_series_detail(cache_key)
        if persisted is not None:
            _series_cache[cache_key] = (now, persisted)
            return persisted

    try:
        detail = await scrape_title_detail(slug, season, source, content_type)
    except Exception as exc:  # noqa: BLE001 - surface as HTTP error, log detail
        logger.exception("Detail scrape failed for slug=%s", slug)
        raise HTTPException(status_code=502, detail=f"Detail scrape failed: {exc}") from exc

    # A title with neither a name nor (for a series) any seasons means the
    # page structure yielded nothing usable - report that rather than
    # handing back an empty page the user cannot act on.
    if not detail.title and not detail.seasons:
        raise HTTPException(
            status_code=404,
            detail="Couldn't read details for this title.",
        )

    # Cache under the season actually selected too, so the follow-up request
    # the frontend makes for that same season is a hit rather than a repeat
    # of this whole browser render. Both the in-process cache and Mongo are
    # written under both keys, so a later request for the season the source
    # defaulted to also hits without a season query param.
    _series_cache[cache_key] = (now, detail)
    await _save_series_detail(cache_key, detail)
    if detail.selected_season is not None:
        default_key = _series_detail_key(source, content_type, slug, detail.selected_season)
        _series_cache[default_key] = (now, detail)
        asyncio.ensure_future(_save_series_detail(default_key, detail))
    return detail


@app.get("/api/favorites")
async def get_favorites(user: ClerkUser = Depends(get_current_user)):
    """The signed-in user's saved movies and TV shows, newest-first."""
    if not settings.MONGODB_URI:
        raise HTTPException(status_code=503, detail="Favorites require MongoDB to be configured")
    items = await favorites.list_favorites(user.user_id)
    return {"items": items}


@app.get("/api/favorites/keys")
async def get_favorite_keys(user: ClerkUser = Depends(get_current_user)):
    """Lightweight membership set ('source:content_type:slug' strings) so a
    grid of many cards can mark which ones are favorited with a single
    request instead of one lookup per card."""
    if not settings.MONGODB_URI:
        return {"keys": []}
    keys = await favorites.get_favorite_keys(user.user_id)
    return {"keys": sorted(keys)}


@app.post("/api/favorites", status_code=201)
async def add_favorite(body: FavoriteIn, user: ClerkUser = Depends(get_current_user)):
    if not settings.MONGODB_URI:
        raise HTTPException(status_code=503, detail="Favorites require MongoDB to be configured")
    if body.content_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="content_type must be 'movie' or 'tv'")
    result = await favorites.add_favorite(user.user_id, body.model_dump())
    return result


@app.delete("/api/favorites/{source}/{content_type}/{slug}", status_code=204)
async def remove_favorite(
    source: str,
    content_type: str,
    slug: str,
    user: ClerkUser = Depends(get_current_user),
):
    if not settings.MONGODB_URI:
        raise HTTPException(status_code=503, detail="Favorites require MongoDB to be configured")
    await favorites.remove_favorite(user.user_id, source, content_type, slug)
    return Response(status_code=204)


@app.get("/api/history")
async def get_history(limit: int = Query(25, ge=1, le=100), user: ClerkUser = Depends(get_current_user)):
    """The signed-in user's "Continue Watching" list, most recently watched
    first. Each entry is one title (see app/watch_history.py's module
    docstring for why), already carrying the last season/episode/position
    watched so the frontend can resume without a second lookup."""
    if not settings.MONGODB_URI:
        return {"items": []}
    items = await watch_history.list_history(user.user_id, limit)
    return {"items": items}


@app.post("/api/history", status_code=204)
async def save_watch_progress(body: WatchProgressIn, user: ClerkUser = Depends(get_current_user)):
    """Upsert a playback checkpoint. Silently a no-op without MongoDB rather
    than an error - history is a convenience feature, and playback itself
    must keep working even if this call fails or is skipped."""
    if not settings.MONGODB_URI:
        return Response(status_code=204)
    if body.content_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="content_type must be 'movie' or 'tv'")
    await watch_history.save_progress(user.user_id, body.model_dump())
    return Response(status_code=204)


@app.delete("/api/history/{source}/{content_type}/{slug}", status_code=204)
async def remove_watch_history(
    source: str,
    content_type: str,
    slug: str,
    user: ClerkUser = Depends(get_current_user),
):
    if not settings.MONGODB_URI:
        raise HTTPException(status_code=503, detail="Watch history requires MongoDB to be configured")
    await watch_history.remove_history(user.user_id, source, content_type, slug)
    return Response(status_code=204)


@app.get("/api/get_embed/{slug}", response_model=EmbedResponse)
async def get_embed(
    slug: str,
    source: str = Query("vidbox"),
    content_type: str = Query("movie"),
    season: int | None = Query(None, ge=0, description="TV only: season number to play"),
    episode: int | None = Query(None, ge=1, description="TV only: episode number to play"),
):
    """Resolve the movie's embed/stream URL via network interception.

    Shares an in-flight/recent capture with /api/prefetch_embed - if the
    frontend already started resolving this slug on hover, this returns
    that result (or waits on it) instead of starting a second browser
    capture. See _get_or_start_embed_capture / EMBED_CACHE_TTL.
    """
    cache_key = _embed_cache_key(slug, source, content_type, season, episode)
    task = _get_or_start_embed_capture(slug, source, content_type, season, episode)
    try:
        embed_url = await task
    except Exception as exc:  # noqa: BLE001
        logger.exception("Embed capture failed for slug=%s", slug)
        _embed_cache.pop(cache_key, None)
        raise HTTPException(status_code=502, detail=f"Embed capture failed: {exc}") from exc

    if not embed_url:
        # Evict the failed capture so the next attempt actually retries.
        #
        # This entry used to be kept deliberately, on the reasoning that a
        # repeat run "would fail the same way". That was wrong, and it was
        # the single biggest cause of titles appearing broken here while
        # playing fine on the source: the capture is genuinely flaky (the
        # Play button needs a variable number of clicks before its React
        # handler attaches), so a failure is often just bad luck on that
        # race. Caching it turned a one-off miss into a guaranteed 120s of
        # instant 404s, which is exactly what a user would see as "this
        # title does not work" - even after retrying.
        #
        # A retry costs the user another wait, but a wrong permanent-looking
        # failure costs them the title entirely.
        _embed_cache.pop(cache_key, None)
        raise HTTPException(
            status_code=404,
            detail=(
                "Couldn't resolve a stream for this title. This is often a "
                "transient capture failure rather than a missing title - "
                "try again."
            ),
        )

    proxied_url = f"/api/proxy_embed?url={quote(embed_url, safe='')}"
    return EmbedResponse(
        slug=slug,
        season=season,
        episode=episode,
        embed_url=embed_url,
        proxied_url=proxied_url,
        is_media=is_direct_media_url(embed_url),
        is_hls=is_hls_manifest_url(embed_url),
    )


@app.post("/api/prefetch_embed/{slug}", status_code=202)
async def prefetch_embed(
    slug: str,
    source: str = Query("vidbox"),
    content_type: str = Query("movie"),
    season: int | None = Query(None, ge=0),
    episode: int | None = Query(None, ge=1),
):
    """Start (or reuse) an embed capture for `slug` without waiting for it -
    called by the frontend on hover so the real click-triggered
    /api/get_embed/{slug} call often finds the result already resolved or
    in flight. Fire-and-forget: errors here are swallowed since the click
    handler will surface them properly if the user actually opens the title.
    """
    _get_or_start_embed_capture(slug, source, content_type, season, episode)
    return {"status": "started"}


@app.get("/api/proxy_embed")
async def proxy_embed(
    request: Request,
    url: str = Query(..., description="Embed URL to proxy, as returned by /api/get_embed"),
    hint: str | None = Query(
        None,
        description=(
            "Optional content-type hint ('manifest' or 'segment') set by "
            "fetch_hls_manifest() when it rewrites a manifest entry. Purely "
            "a dispatch shortcut - NOT trusted for security purposes, since "
            "it's a client-visible query param a caller could set to "
            "anything; is_allowed_embed_url()'s host allowlist and "
            "private-IP check apply identically regardless of its value."
        ),
    ),
):
    """Fetch the embed server-side with spoofed headers and strip the
    framing-restriction headers so it renders locally.

    Three cases, in order:
      * .m3u8 (HLS playlist) - fetched and text-rewritten so every segment/
        variant/key URI it references also routes through this endpoint.
      * other direct media (.mp4/.ts/...) - streamed with Range support for
        <video> seeking/scrubbing.
      * HTML embed pages - buffered and asset-URL-rewritten for an <iframe>.

    Some CDNs disguise segment URLs with a misleading extension (e.g.
    ".html") to dodge naive "is this media" checks - `hint`, when present,
    short-circuits the extension sniffing below for URLs we ourselves
    generated while rewriting an already-validated manifest (see
    proxy._proxy_media_url), rather than misclassifying those as an HTML
    embed page.
    """
    decoded_url = unquote(url)

    if not is_allowed_embed_url(decoded_url):
        raise HTTPException(status_code=403, detail="URL is not an allowed embed URL")

    if hint == "manifest" or (hint is None and is_hls_manifest_url(decoded_url)):
        try:
            content_type, body = await fetch_hls_manifest(decoded_url)
        except StreamAuthError as exc:
            # The link's token expired or was rotated away. Drop any cached
            # capture that produced it so the next resolve starts a fresh
            # one, and tell the client to re-resolve rather than reporting
            # a generic upstream failure it can't act on.
            _invalidate_cached_embed(decoded_url)
            logger.info("Stale stream link for %s: %s", decoded_url, exc)
            raise HTTPException(
                status_code=410,
                detail=f"Stream link expired, please reload the title: {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("HLS manifest fetch failed for %s", decoded_url)
            raise HTTPException(status_code=502, detail=f"Failed to fetch playlist: {exc}") from exc

        return Response(
            content=body,
            headers={"Content-Type": content_type, "Cache-Control": "no-store"},
        )

    if hint == "segment" or (hint is None and is_direct_media_url(decoded_url)):
        try:
            status_code, upstream_headers, byte_iter, aclose = await stream_media(
                decoded_url, dict(request.headers)
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Media stream failed for %s", decoded_url)
            raise HTTPException(status_code=502, detail=f"Failed to stream media: {exc}") from exc

        # A token that expires mid-playback shows up here, on a segment,
        # rather than on the manifest. Report it the same way the manifest
        # path does (410 + cache eviction) so the player's stale-link
        # recovery fires instead of stalling on a dead segment.
        if status_code in (401, 403):
            await aclose()
            _invalidate_cached_embed(decoded_url)
            logger.info("Stale stream link on segment %s (%s)", decoded_url, status_code)
            raise HTTPException(
                status_code=410,
                detail="Stream link expired, please reload the title",
            )

        headers = {**upstream_headers, "Cache-Control": "no-store"}
        # Segment CDNs that disguise .ts files as ".html" also return the
        # matching bogus content-type. Passing "text/html" through for what
        # is really binary MPEG-TS is wrong on its face and invites strict
        # players to reject the segment, so override it for anything we
        # dispatched here as a segment.
        if hint == "segment" and "html" in headers.get("content-type", "").lower():
            headers["content-type"] = "video/mp2t"

        return StreamingResponse(
            byte_iter,
            status_code=status_code,
            headers=headers,
            background=BackgroundTask(aclose),
        )

    try:
        content_type, body = await fetch_sanitized_embed(decoded_url)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Proxy fetch failed for %s", decoded_url)
        raise HTTPException(status_code=502, detail=f"Failed to fetch embed: {exc}") from exc

    headers = build_proxy_response_headers(content_type)
    embed_host = (urlparse(decoded_url).hostname or "").lower()
    bingeflix_hosts = {host.lower() for host in settings.BINGEFLIX_EMBED_HOSTS}
    if "html" in content_type.lower() and embed_host in bingeflix_hosts:
        headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https: data: blob:; "
            "font-src 'self' data:; "
            "media-src 'self' https: blob:; "
            "connect-src 'self' https:; "
            "frame-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; "
            "navigate-to 'self'"
        )
    return Response(content=body, headers=headers)


@app.get("/_next/{asset_path:path}")
async def proxy_player_asset(asset_path: str, request: Request):
    """Serve only configured player Next.js assets locally."""
    try:
        content_type, body = await fetch_player_asset(asset_path, request.url.query)
    except Exception as exc:  # noqa: BLE001 - upstream asset failures are 404s here
        logger.warning("Player asset fetch failed for /_next/%s: %s", asset_path, exc)
        raise HTTPException(status_code=404, detail="Player asset not found") from exc
    return Response(
        content=body,
        headers={"Content-Type": content_type, "Cache-Control": "no-store"},
    )


@app.get("/api/proxy_image")
async def proxy_image(
    url: str = Query(..., description="Poster/thumbnail image URL to proxy"),
):
    """Fetch a thumbnail server-side with a spoofed Referer.

    Some image CDNs rate-limit anonymous hotlink bursts with no Referer,
    which the browser won't send cross-origin from localhost - fetching
    server-side avoids the 429s that causes on catalog pages with many
    poster thumbnails.
    """
    decoded_url = unquote(url)

    if not is_allowed_image_url(decoded_url):
        raise HTTPException(status_code=403, detail="URL is not an allowed image URL")

    try:
        content_type, body = await fetch_image(decoded_url)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Image proxy fetch failed for %s", decoded_url)
        raise HTTPException(status_code=502, detail=f"Failed to fetch image: {exc}") from exc

    return Response(
        content=body,
        headers={"Content-Type": content_type, "Cache-Control": "public, max-age=86400"},
    )


# TEMPORARY diagnostic route - remove alongside scraper_embed._dump_debug_snapshot
# once the HF-container Vidbox failure is understood. Lists/serves whatever
# capture_embed_url() saved to debug_snapshots/ on a failed capture.
@app.get("/api/_debug/snapshots")
async def list_debug_snapshots():
    import os
    d = "debug_snapshots"
    if not os.path.isdir(d):
        return {"files": []}
    return {"files": sorted(os.listdir(d))}


@app.get("/api/_debug/snapshots/{filename}")
async def get_debug_snapshot(filename: str):
    import os
    d = "debug_snapshots"
    path = os.path.join(d, filename)
    if ".." in filename or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")
    media_type = "image/png" if filename.endswith(".png") else "text/html"
    with open(path, "rb") as f:
        body = f.read()
    return Response(content=body, media_type=media_type)


@app.get("/api/subtitles/{slug}")
async def get_subtitles(
    slug: str,
    content_type: str = Query("movie", pattern="^(movie|tv)$"),
    season: int | None = Query(None, ge=0),
    episode: int | None = Query(None, ge=1),
):
    """Return whichever configured subtitle languages actually exist for
    this title, as a list of {lang, url}. The URLs are built server-side
    from SUBTITLE_URL_TEMPLATE (see app/config.py) and point directly at the
    subtitle CDN - they're public, unauthenticated .vtt files, so the
    frontend fetches them directly rather than through a second proxy hop.
    A language 404ing just means this title has no subtitles in it, which
    is common - not every title has subtitles available at all.

    Availability rarely changes once known, so each (slug, content_type,
    season, episode, lang) probe result is persisted in Mongo - a language
    that 404s stays a known miss, and one that hits stays a known hit,
    either way skipping the CDN round-trip on every future request for the
    same title/episode.
    """
    # TV subtitles are keyed per episode under their own path on the CDN.
    # Using the movie template for a series does not 404 - it returns a
    # different title's subtitles for the same numeric id - so routing by
    # content_type here is what stops a series showing wrong subtitles.
    if content_type == "tv":
        template = settings.TV_SUBTITLE_URL_TEMPLATE
    else:
        template = settings.SUBTITLE_URL_TEMPLATE
    if not template:
        return {"tracks": []}

    async def probe(lang: str) -> dict | None:
        url = template.format(
            slug=slug, lang=lang, season=season or 1, episode=episode or 1
        )
        cache_key = f"{content_type}:{slug}:{season or 1}:{episode or 1}:{lang}"
        cached = await _load_subtitle_probe(cache_key)
        if cached is not None:
            return {"lang": lang, "url": url} if cached else None
        try:
            body = await fetch_subtitle(url)
        except Exception:  # noqa: BLE001 - a subtitle fetch failure shouldn't break playback
            logger.exception("Subtitle probe failed for %s/%s", slug, lang)
            return None
        await _save_subtitle_probe(cache_key, available=bool(body))
        return {"lang": lang, "url": url} if body else None

    results = await asyncio.gather(*(probe(lang) for lang in settings.SUBTITLE_LANGUAGES))
    return {"tracks": [t for t in results if t]}


# Serve the built React/Vite frontend at / (run `npm run build` in
# frontend/ first - see frontend/README.md). During frontend development,
# run `npm run dev` in frontend/ instead and use its dev server (which
# proxies /api to this backend - see frontend/vite.config.js) rather than
# this mount.
#
# Conditional: when the backend is deployed standalone (e.g. a Hugging Face
# Space, with the frontend deployed separately to Netlify - see
# frontend/.env.example's VITE_API_BASE_URL), frontend/dist never exists
# here at all. StaticFiles raises on mount if its directory is missing, so
# this would otherwise crash the app on startup in that deployment.
#
# Resolved relative to this file (not the process's cwd): plain `python
# run.py` from the project root made a bare "frontend/dist" work by
# accident, but a PyInstaller-frozen exe can be launched from any working
# directory (wherever the user double-clicked it), and sys._MEIPASS is
# where PyInstaller actually extracts bundled data at runtime - a relative
# path would silently fail to find the frontend in that case.
import os
import sys

_frontend_dist = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(__file__))), "frontend", "dist")

if os.path.isdir(_frontend_dist):
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
