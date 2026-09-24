"""
TMDB metadata client.

Why this exists: the source's slugs ARE TMDB ids (verified: 1396=Breaking
Bad, 108978=Reacher, 4614=NCIS), and the site itself just mirrors TMDB
payloads into its pages. So every piece of *metadata* - title, overview,
rating, genres, poster, backdrop, seasons, episodes, cast - can be fetched
here in ~200ms instead of costing a ~10s headless browser render.

What this module deliberately does NOT do:
  * decide which titles exist. That comes from scraping the source's
    catalog - TMDB has no idea what is actually playable there.
  * resolve stream URLs. Also scraping; see scraper_embed.py.

So TMDB answers "what is this title?" and the scraper answers "what can we
watch, and where is the video?". Keeping that split is what lets the app
show real, playable titles while still having rich metadata.

Everything degrades rather than fails: with no credentials configured, or
if TMDB is unreachable, every function returns None and callers fall back
to the existing scrape path.

Persistence: responses are also cached in MongoDB (`tmdb_cache` collection)
behind the in-process dict, so a restart doesn't re-pay the ~200ms request
for a title thousands of catalog entries have already resolved. The TTL is
the same in both layers - Mongo just survives what the dict can't.
"""
import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Responses are cached in-process keyed by request path+params. Metadata
# changes rarely, so this TTL is long (default 6h) - unlike anything tied
# to a stream token, which expires in hours and must never be cached that
# aggressively.
_cache: dict[str, tuple[float, Any]] = {}

# Bounded so a long-running server cannot grow this without limit.
_CACHE_LIMIT = 4096


def _mongo_cache_collection():
    from app.db import get_database

    return get_database()["tmdb_cache"]


async def _mongo_get(cache_key: str) -> tuple[bool, Any]:
    """Return (hit, value) from the Mongo-backed cache layer.

    hit=False on a miss, on an expired entry, or if Mongo isn't configured -
    callers treat all three identically (fall through to a live request).
    """
    if not settings.MONGODB_URI:
        return False, None
    try:
        doc = await _mongo_cache_collection().find_one({"_id": cache_key})
    except Exception:  # noqa: BLE001 - Mongo being unavailable must not break TMDB lookups
        logger.warning("tmdb_cache lookup failed for %s", cache_key, exc_info=True)
        return False, None
    if not doc:
        return False, None
    if (time.time() - doc.get("cached_at", 0)) >= settings.TMDB_CACHE_TTL:
        return False, None
    return True, doc.get("value")


async def _mongo_set(cache_key: str, value: Any) -> None:
    if not settings.MONGODB_URI:
        return
    from app.db import expires_at

    try:
        await _mongo_cache_collection().update_one(
            {"_id": cache_key},
            {
                "$set": {
                    "value": value,
                    "cached_at": time.time(),
                    "expires_at": expires_at(settings.TMDB_CACHE_TTL),
                }
            },
            upsert=True,
        )
    except Exception:  # noqa: BLE001 - persistence is an optimisation only
        logger.warning("tmdb_cache save failed for %s", cache_key, exc_info=True)

# One shared client: creating an AsyncClient per call would give up
# connection pooling and TLS session reuse, which is most of the speed
# advantage TMDB has over scraping in the first place.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


def is_enabled() -> bool:
    """True if TMDB credentials are configured.

    Callers use this to decide whether to try TMDB at all; when False the
    app behaves exactly as it did before TMDB existed.
    """
    return bool(settings.TMDB_READ_TOKEN or settings.TMDB_API_KEY)


async def _get_client() -> httpx.AsyncClient:
    """Return the shared HTTP client, creating it on first use."""
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    async with _client_lock:
        if _client is not None and not _client.is_closed:
            return _client
        headers = {"Accept": "application/json"}
        # The v4 read token is a bearer credential and is preferred when
        # both are set; the v3 key travels as a query param instead (added
        # in _request), since TMDB accepts only one of the two schemes.
        if settings.TMDB_READ_TOKEN:
            headers["Authorization"] = f"Bearer {settings.TMDB_READ_TOKEN}"
        _client = httpx.AsyncClient(
            base_url=settings.TMDB_BASE_URL,
            headers=headers,
            timeout=settings.TMDB_TIMEOUT_S,
        )
        return _client


async def _request(path: str, **params: Any) -> dict | None:
    """GET `path` from TMDB, cached. Returns None on any failure.

    Returning None rather than raising is deliberate: every caller has a
    scrape-based fallback, so a TMDB outage should degrade the app to its
    previous behaviour rather than surface an error to the user.
    """
    if not is_enabled():
        return None

    params = {k: v for k, v in params.items() if v is not None}
    params.setdefault("language", settings.TMDB_LANGUAGE)
    # v3 key auth rides as a query param; harmless (and unused) when the
    # bearer token is what actually authenticates the request.
    if not settings.TMDB_READ_TOKEN and settings.TMDB_API_KEY:
        params["api_key"] = settings.TMDB_API_KEY

    cache_key = f"{path}?{sorted(params.items())}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < settings.TMDB_CACHE_TTL:
        return cached[1]

    # Mongo-backed layer: survives a restart, so a title thousands of other
    # requests already resolved doesn't cost a fresh TMDB round-trip just
    # because the in-process dict was wiped. Populate the in-process dict on
    # a hit too, so the next call within this process skips Mongo entirely.
    hit, value = await _mongo_get(cache_key)
    if hit:
        _cache[cache_key] = (now, value)
        return value

    try:
        client = await _get_client()
        response = await client.get(path, params=params)
    except Exception:  # noqa: BLE001 - network failure must not break callers
        logger.warning("TMDB request failed for %s", path, exc_info=True)
        return None

    if response.status_code == 404:
        # A title genuinely absent from TMDB. Cache the miss so a catalog
        # full of obscure titles doesn't re-request each one every time.
        _store(cache_key, now, None)
        await _mongo_set(cache_key, None)
        return None
    if response.status_code != 200:
        logger.warning("TMDB %s returned %s", path, response.status_code)
        return None

    try:
        data = response.json()
    except ValueError:
        logger.warning("TMDB %s returned non-JSON", path)
        return None

    _store(cache_key, now, data)
    await _mongo_set(cache_key, data)
    return data


def _store(key: str, now: float, value: Any) -> None:
    """Cache `value`, evicting the oldest entries past the size limit."""
    _cache[key] = (now, value)
    if len(_cache) > _CACHE_LIMIT:
        for stale in sorted(_cache, key=lambda k: _cache[k][0])[: len(_cache) - _CACHE_LIMIT]:
            _cache.pop(stale, None)


async def get_title(tmdb_id: str | int, content_type: str = "movie") -> dict | None:
    """Full metadata for one title, in a single request.

    `content_type` picks TMDB's /movie or /tv endpoint - the two return the
    same information under different key names, which normalise_title()
    below reconciles. append_to_response bundles cast, videos (trailer) and
    similar titles into this one call rather than three - each would
    otherwise be a separate round-trip per detail page load.

    TV asks for aggregate_credits rather than plain credits: a series'
    plain `/tv/{id}` credits only reflects its LATEST season's cast, which
    drops anyone who left earlier in a long-running show (verified: The
    Walking Dead's plain credits omit Andrew Lincoln/Rick Grimes entirely,
    since he'd left before the final season). aggregate_credits instead
    merges cast across every season, which is what "who's in this show"
    actually means. Movies have no such distinction - a movie has one cast,
    so plain `credits` is both correct and all that endpoint supports.
    """
    is_tv = content_type == "tv"
    credits_param = "aggregate_credits" if is_tv else "credits"
    path = f"/{'tv' if is_tv else 'movie'}/{tmdb_id}"
    return await _request(path, append_to_response=f"{credits_param},videos,similar")


async def get_season(tmdb_id: str | int, season_number: int) -> dict | None:
    """One season's episode list, with titles, overviews and stills.

    This is the call that replaces the expensive half of the old scrape:
    episodes are not in the source's page payload, so reading them used to
    mean clicking through to the watch page and parsing rendered DOM.
    """
    return await _request(f"/tv/{tmdb_id}/season/{season_number}")


def image_url(path: str | None, size: str = "w500") -> str:
    """Build a full TMDB image URL from a bare path."""
    if not path:
        return ""
    if not path.startswith("/"):
        return path
    return f"https://image.tmdb.org/t/p/{size}{path}"


_CAST_LIMIT = 12  # top-billed only - a full credits list is dozens of names deep


def _extract_cast(data: dict) -> list[dict]:
    """Top cast members, present when get_title() asked for credits (movie)
    or aggregate_credits (TV) - see get_title()'s docstring for why TV uses
    the latter.

    The two shapes differ: plain credits.cast has a flat `character` and is
    already ordered by billing, so taking the head is correct as-is.
    aggregate_credits.cast instead nests `roles: [{character,
    episode_count}, ...]` (a recast character has more than one role entry)
    with no reliable top-level ordering for a long-running show - sorting by
    total episode count surfaces the show's actual leads (Rick Grimes/Andrew
    Lincoln, absent from plain credits, sorts to the top here) rather than
    whoever TMDB's `order` field happens to favour.
    """
    aggregate = data.get("aggregate_credits")
    if aggregate is not None:
        cast = aggregate.get("cast") or []
        entries = []
        for member in cast:
            if not isinstance(member, dict) or not member.get("name"):
                continue
            roles = member.get("roles") or []
            episode_count = sum(r.get("episode_count", 0) for r in roles if isinstance(r, dict))
            character = roles[0].get("character", "") if roles else ""
            entries.append((episode_count, member, character))
        entries.sort(key=lambda e: e[0], reverse=True)
        return [
            {
                "name": member.get("name") or "",
                "character": character,
                "profile_url": image_url(member.get("profile_path"), "w185"),
            }
            for _, member, character in entries[:_CAST_LIMIT]
        ]

    cast = (data.get("credits") or {}).get("cast") or []
    return [
        {
            "name": member.get("name") or "",
            "character": member.get("character") or "",
            "profile_url": image_url(member.get("profile_path"), "w185"),
        }
        for member in cast[:_CAST_LIMIT]
        if isinstance(member, dict) and member.get("name")
    ]


def _extract_trailer_key(data: dict) -> str:
    """The bare YouTube id of the first official trailer, present when
    get_title() asked for append_to_response=videos. Prefers an actual
    "Trailer" over a "Teaser"/clip, and YouTube specifically since that's
    the only site the frontend knows how to embed."""
    videos = (data.get("videos") or {}).get("results") or []
    youtube = [v for v in videos if isinstance(v, dict) and v.get("site") == "YouTube" and v.get("key")]
    trailer = next((v for v in youtube if v.get("type") == "Trailer"), None)
    return (trailer or (youtube[0] if youtube else {})).get("key", "")


_SIMILAR_LIMIT = 12


def _extract_similar(data: dict, content_type: str) -> list[dict]:
    """Related titles from similar.results, present when get_title() asked
    for append_to_response=similar. Each entry's TMDB id doubles as this
    source's slug - see app/tmdb.py's module docstring - so no extra lookup
    is needed to make these clickable."""
    results = (data.get("similar") or {}).get("results") or []
    out = []
    for item in results[:_SIMILAR_LIMIT]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        date = item.get("first_air_date") or item.get("release_date") or ""
        rating = item.get("vote_average")
        out.append(
            {
                "slug": str(item["id"]),
                "title": item.get("name") or item.get("title") or "",
                "poster_url": image_url(item.get("poster_path"), "w342"),
                "rating": round(float(rating), 1) if isinstance(rating, (int, float)) and rating else None,
                "year": date[:4] if date else "",
                "content_type": content_type,
            }
        )
    return out


def normalise_title(data: dict, content_type: str) -> dict:
    """Flatten a TMDB title object into the fields the app renders.

    Movies and series carry the same information under different keys
    (title/name, release_date/first_air_date, runtime/episode_run_time),
    so reconciling here keeps that difference out of the endpoints and the
    frontend.
    """
    date = data.get("first_air_date") or data.get("release_date") or ""
    rating = data.get("vote_average")
    # A movie has a single `runtime` (minutes); a series instead lists
    # `episode_run_time` as an array (one entry per typical episode length
    # across its run) - take the first as the show's typical length.
    runtime = data.get("runtime")
    if runtime is None:
        episode_runtimes = data.get("episode_run_time") or []
        runtime = episode_runtimes[0] if episode_runtimes else None
    return {
        "title": data.get("name") or data.get("title") or data.get("original_name") or "",
        "overview": data.get("overview") or "",
        "backdrop_url": image_url(data.get("backdrop_path"), "original"),
        "poster_url": image_url(data.get("poster_path"), "w500"),
        # TMDB reports 0 for "no votes yet", which must render as the
        # absence of a rating rather than as a genuine 0.0.
        "rating": round(float(rating), 1) if isinstance(rating, (int, float)) and rating else None,
        "year": date[:4] if date else "",
        "genres": [g["name"] for g in data.get("genres", []) if isinstance(g, dict) and g.get("name")],
        "genre_ids": [g["id"] for g in data.get("genres", []) if isinstance(g, dict) and g.get("id")],
        "tagline": data.get("tagline") or "",
        "runtime": int(runtime) if isinstance(runtime, (int, float)) and runtime else None,
        "status": data.get("status") or "",
        "cast": _extract_cast(data),
        "trailer_key": _extract_trailer_key(data),
        "similar": _extract_similar(data, content_type),
        "content_type": content_type,
    }


def normalise_episode(data: dict, season_number: int) -> dict:
    """Flatten one TMDB episode object."""
    return {
        "season_number": data.get("season_number", season_number),
        "episode_number": data.get("episode_number", 0),
        "title": data.get("name") or "",
        "overview": data.get("overview") or "",
        "still_url": image_url(data.get("still_path"), "w500"),
        "air_date": data.get("air_date") or "",
        "runtime": data.get("runtime"),
    }


async def close() -> None:
    """Close the shared client. Called from the app's shutdown hook."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
