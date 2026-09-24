"""
Season/episode scraping for TV series.

Why this exists: the rest of the app treats every title as a single
playable thing, which is true for a movie and false for a series. Without
this module the TV path had no notion of seasons or episodes at all - the
embed capture hardcoded season 1 / episode 1, so every series played its
pilot no matter what the user wanted, and there was nothing to select from
in the first place.

Where the data comes from (Vidbox):
  * Seasons - the show page (/tv/{slug}) is a Next.js app that inlines the
    series' TMDB payload into its RSC flight data
    (self.__next_f.push([1,"..."])). That payload carries the full
    `seasons` array, so seasons need no UI driving at all: fetch the page,
    unescape the flight chunks, pull the array out.
  * Episodes - deliberately NOT in that payload. The episode list is
    rendered client-side on the watch page, one season at a time, from data
    the page already holds; selecting a season fires no network request at
    all (verified: switching seasons produces zero XHR/fetch). So episodes
    have to be read out of the rendered DOM after driving the season
    picker, which is what fetch_season_episodes() does.

That asymmetry is why the API is split: /api/series returns seasons cheaply
from the payload and fills in episodes only for the one season asked for,
rather than driving the browser N times to render every season up front.
"""
import json
import logging
import re
from urllib.parse import urljoin

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app import tmdb
from app.browser_pool import new_context
from app.config import settings
from app.models import CastMember, Episode, Season, SimilarTitle, TitleDetail

logger = logging.getLogger(__name__)

# TMDB image base used by the source for episode stills / season posters.
_TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# TMDB's genre ids -> names. A movie's payload on this source carries only
# `genre_ids` (it uses TMDB's compact list shape), while a series carries a
# full `genres` array of {id, name}. Rather than make an API call for two
# labels, map the ids here: TMDB's genre table is small and effectively
# fixed, and an id missing from it simply renders as no chip.
_TMDB_GENRES: dict[int, str] = {
    12: "Adventure", 14: "Fantasy", 16: "Animation", 18: "Drama", 27: "Horror",
    28: "Action", 35: "Comedy", 36: "History", 37: "Western", 53: "Thriller",
    80: "Crime", 99: "Documentary", 878: "Science Fiction", 9648: "Mystery",
    10402: "Music", 10749: "Romance", 10751: "Family", 10752: "War",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News",
    10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap",
    10767: "Talk", 10768: "War & Politics", 10770: "TV Movie",
}

# The source wraps its images in an image-resizing CDN
# (wsrv.nl/?url=<real url>&output=webp...). The real TMDB URL is the `url`
# query value; we want that rather than the wrapper, since /api/proxy_image
# has its own allowlist and TMDB is already on it.
_WSRV_URL_RE = re.compile(r"[?&]url=([^&]+)")


def _extract_flight_payload(html: str) -> str:
    """Return the concatenated, unescaped Next.js RSC flight payload.

    Next streams its server-rendered data as a series of
    `self.__next_f.push([1,"<json-ish chunk>"])` script calls, where each
    chunk is a JS string literal (so \\" for quotes, \n for newlines).
    Joining the raw chunks before unescaping matters: a single JSON value
    can be split across two pushes, and unescaping them individually would
    corrupt anything straddling the boundary.
    """
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    if not chunks:
        return ""
    blob = "".join(chunks)
    try:
        # The chunks are JS string-literal bodies; wrapping in quotes and
        # letting the JSON parser do the unescaping handles \" \ \n \uXXXX
        # correctly, which a hand-rolled .replace() chain does not.
        return json.loads(f'"{blob}"')
    except json.JSONDecodeError:
        # Fall back to a lenient decode rather than failing the whole
        # request - callers degrade to "no seasons found", not a 500.
        logger.warning("Flight payload did not parse as a JSON string; using lenient decode")
        return blob.encode("utf-8", "ignore").decode("unicode_escape", "ignore")


def _find_json_array(blob: str, key: str) -> list | None:
    """Extract the JSON array that follows `"<key>":` in `blob`.

    The flight payload is not valid JSON as a whole (it is a stream of
    partial values with React element placeholders), so it cannot simply be
    json.loads()'d. Scanning for the key and then bracket-matching the array
    that follows lets us recover the one well-formed sub-value we care
    about without needing the surrounding structure to be parseable.
    """
    marker = f'"{key}":'
    start = blob.find(marker)
    if start == -1:
        return None
    start = blob.find("[", start)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(blob)):
        ch = blob[i]
        # String-awareness is required: episode overviews routinely contain
        # brackets, which would otherwise unbalance the depth count.
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(blob[start : i + 1])
                except json.JSONDecodeError:
                    logger.warning("Bracket-matched %r array did not parse", key)
                    return None
    return None


def _find_enclosing_object(blob: str, anchor: int) -> dict | None:
    """Parse the JSON object that encloses `anchor`.

    Needed because generic keys like "name" and "overview" appear dozens of
    times in the flight payload - on meta tags, genres, cast members, the
    network, the last-aired episode - so no positional search for a bare key
    finds the series' own value reliably. Scanning forward returns
    "theme-color" as the title; scanning backward from the series object
    returns "AMC" (its network). Both were observed.

    Instead, walk back to the `{` that opens the object containing a key
    unique to the series (e.g. "number_of_seasons") and parse that object,
    so its fields are read as the structure they actually are.
    """
    if anchor < 0:
        return None

    # Find the opening brace by scanning FORWARD from the start, tracking
    # which braces are still open, rather than walking backwards from the
    # anchor. Walking backwards has to reason about string literals in
    # reverse (a quote read right-to-left is ambiguous - opening or
    # closing?), which mis-parsed overviews containing braces or quotes and
    # landed on the wrong object.
    open_positions: list[int] = []
    in_string = False
    escaped = False
    i = -1
    for pos in range(anchor):
        ch = blob[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            open_positions.append(pos)
        elif ch == "}":
            if open_positions:
                open_positions.pop()
    if not open_positions:
        return None
    # The innermost brace still open at the anchor opens the object the key
    # belongs to.
    i = open_positions[-1]

    # Then bracket-match forward from that brace to the object's end.
    depth = 0
    in_string = False
    escaped = False
    for j in range(i, len(blob)):
        ch = blob[j]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(blob[i : j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _tmdb_image(path: str | None, size: str = "w500") -> str:
    """Build a full TMDB image URL from a bare TMDB path.

    `size` is TMDB's own path segment - backdrops are rendered full-bleed
    behind the hero, so they ask for "original" rather than the w500 used
    for posters and stills.
    """
    if not path:
        return ""
    if not path.startswith("/"):
        return path
    return f"https://image.tmdb.org/t/p/{size}{path}"


def _unwrap_image_url(src: str | None) -> str:
    """Return the real image URL behind the source's wsrv.nl resizer."""
    if not src:
        return ""
    match = _WSRV_URL_RE.search(src)
    if match:
        from urllib.parse import unquote

        return unquote(match.group(1))
    return src


def parse_detail(html: str, content_type: str = "tv") -> tuple[dict, list[Season]]:
    """Pull the title's own TMDB object and its seasons out of a rendered
    detail page.

    Returns the raw object rather than a model so the caller can map it -
    movies and series share almost every field but name them differently
    (name/title, first_air_date/release_date).
    """
    blob = _extract_flight_payload(html)
    if not blob:
        return {}, []

    # "number_of_seasons" appears only on a series object; a movie page has
    # no such key, so anchor on something both have. "vote_average" occurs on
    # episodes and cast members too, but "poster_path" alongside a
    # "genres" array is specific enough to land on the title itself.
    anchor = blob.find('"number_of_seasons"')
    if anchor == -1:
        anchor = blob.find('"release_date"')
    if anchor == -1:
        anchor = blob.find('"poster_path"')
    detail = _find_enclosing_object(blob, anchor) or {}

    raw_seasons = _find_json_array(blob, "seasons") or []
    seasons: list[Season] = []
    for entry in raw_seasons:
        if not isinstance(entry, dict) or "season_number" not in entry:
            continue
        seasons.append(
            Season(
                season_number=entry.get("season_number", 0),
                name=entry.get("name") or f"Season {entry.get('season_number', 0)}",
                episode_count=entry.get("episode_count") or 0,
                overview=entry.get("overview") or "",
                poster_url=_tmdb_image(entry.get("poster_path")),
                air_date=entry.get("air_date") or "",
            )
        )

    # Seasons with no episodes at all are noise in a picker - they render as
    # a selectable season that then shows an empty episode list.
    seasons = [s for s in seasons if s.episode_count > 0]
    seasons.sort(key=lambda s: s.season_number)

    return detail, seasons


def _genre_names(detail: dict) -> list[str]:
    """Genre labels for a title, from whichever shape its payload uses.

    A series carries `genres` as [{id, name}]; a movie carries only
    `genre_ids` as bare TMDB ids, which is why a name lookup is needed for
    one and not the other.
    """
    genres = detail.get("genres")
    if isinstance(genres, list) and genres:
        return [g.get("name", "") for g in genres if isinstance(g, dict) and g.get("name")]
    return [
        _TMDB_GENRES[gid]
        for gid in detail.get("genre_ids", []) or []
        if isinstance(gid, int) and gid in _TMDB_GENRES
    ]


def _detail_from_payload(
    detail: dict, slug: str, source: str, content_type: str, seasons: list[Season]
) -> TitleDetail:
    """Map a raw TMDB title object onto TitleDetail.

    Movies and series carry the same information under different keys
    (title/name, release_date/first_air_date), so normalising here keeps
    that difference out of both the endpoint and the frontend.
    """
    title = detail.get("name") or detail.get("title") or detail.get("original_name") or ""
    date = detail.get("first_air_date") or detail.get("release_date") or ""
    rating = detail.get("vote_average")
    return TitleDetail(
        slug=slug,
        title=title,
        source=source,
        content_type=content_type,
        overview=detail.get("overview") or "",
        backdrop_url=_tmdb_image(detail.get("backdrop_path"), "original"),
        poster_url=_tmdb_image(detail.get("poster_path")),
        # TMDB reports 0 for "no votes yet", which would render as a 0.0
        # rating rather than as the absence of one.
        rating=round(float(rating), 1) if isinstance(rating, (int, float)) and rating else None,
        year=date[:4] if date else "",
        genres=_genre_names(detail),
        tagline=detail.get("tagline") or "",
        seasons=seasons,
    )


async def _open_watch_page(page, slug: str) -> bool:
    """Navigate from a series' show page to its watch page.

    The watch page is reached by clicking Play rather than by URL: the
    source routes to it client-side with a state payload
    ({type, id, season, episode}) and /watch/tv answers 404 to a direct GET,
    with or without query parameters (verified against several URL shapes).
    So there is no URL we could construct instead of driving the button.

    Same hydration caveat as the embed capture: the button renders before
    its React handler attaches, so the first click is often a no-op - hence
    clicking until the route actually changes.
    """
    try:
        play_button = page.get_by_role("button", name=re.compile(r"^\s*play\s*$", re.I)).first
        await play_button.wait_for(timeout=settings.SELECTOR_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - caller degrades to seasons-without-episodes
        logger.warning("Play button never appeared for slug=%s", slug)
        return False

    for _ in range(settings.VIDBOX_PLAY_CLICK_ATTEMPTS):
        try:
            await play_button.click(force=True)
        except Exception:  # noqa: BLE001 - element can detach as the route changes
            pass
        try:
            await page.wait_for_url(
                re.compile(r"/watch/"), timeout=settings.VIDBOX_PLAYER_MOUNT_TIMEOUT_MS
            )
            return True
        except PlaywrightTimeoutError:
            continue

    logger.warning("Never reached the watch page for slug=%s", slug)
    return False


async def _select_season(page, season_number: int) -> None:
    """Switch the watch page's episode list to `season_number`.

    Matches the season by its exact number rather than by list position:
    a "Specials" season (season 0) is listed first on shows that have one,
    so index N is not season N.
    """
    try:
        combobox = page.get_by_role("combobox").first
        await combobox.wait_for(timeout=settings.SELECTOR_TIMEOUT_MS)
        current = (await combobox.inner_text()).strip()
        # Already showing the season we want - opening the menu would only
        # risk leaving it open over the episode list.
        if re.fullmatch(rf"Season\s*{season_number}", current, re.I):
            return
        await combobox.click()
        option = (
            page.get_by_role("option")
            .filter(has_text=re.compile(rf"^\s*Season\s*{season_number}\s*$", re.I))
            .first
        )
        await option.wait_for(timeout=settings.SELECTOR_TIMEOUT_MS)
        await option.click()
        # The list re-renders from data already in the page (no network), so
        # a short settle is enough - there is no request to wait on.
        await page.wait_for_timeout(1200)
    except Exception:  # noqa: BLE001 - fall through with whatever season is shown
        logger.warning("Could not select season %s; using the default season", season_number)


async def _parse_episode_cards(page, season_number: int) -> list[Episode]:
    """Read the rendered episode cards for the currently-selected season.

    Structure per card (verified against the live page):
        <a><div><img alt="{episode title}" src="{wsrv-wrapped still}">
             <div>...<div>{episode number}</div></div></div>
           <div><h2>{title}</h2><p>{overview}</p></div></a>
    Anchored on the "Episodes" heading rather than on a global selector so
    unrelated cards elsewhere on the page cannot leak into the list.
    """
    heading = page.locator("h1", has_text=re.compile(r"^\s*Episodes\s*$", re.I)).first
    try:
        await heading.wait_for(timeout=settings.SELECTOR_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        logger.warning("Episodes section never rendered")
        return []

    section = heading.locator("xpath=../..")
    cards = section.locator("a:has(h2)")
    episodes: list[Episode] = []
    for i in range(await cards.count()):
        card = cards.nth(i)
        try:
            title = (await card.locator("h2").first.inner_text()).strip()
        except Exception:  # noqa: BLE001 - skip a card that vanished mid-read
            continue

        overview = ""
        paragraph = card.locator("p").first
        if await paragraph.count():
            overview = (await paragraph.inner_text()).strip()

        still_url = ""
        image = card.locator("img").first
        if await image.count():
            still_url = _unwrap_image_url(await image.get_attribute("src"))

        # The episode number is a small badge overlaying the still. Read it
        # where possible rather than trusting position, then fall back to
        # the card's index - a season whose list starts at episode 1 and is
        # rendered in order makes that a safe fallback, and it keeps a
        # badge-less card playable instead of dropping it.
        number = i + 1
        badge = card.locator("div.rounded-br-md").first
        if await badge.count():
            match = re.search(r"\d+", (await badge.inner_text()).strip())
            if match:
                number = int(match.group())

        episodes.append(
            Episode(
                season_number=season_number,
                episode_number=number,
                title=title,
                overview=overview,
                still_url=still_url,
            )
        )

    episodes.sort(key=lambda e: e.episode_number)
    return episodes


async def _detail_from_tmdb(
    slug: str, season: int | None, source: str, content_type: str
) -> TitleDetail | None:
    """Build the detail response from TMDB instead of scraping.

    This is the fast path, and it is worth a lot: the scrape below has to
    render the show page AND click through to the watch page just to read
    an episode list, which costs ~10s. TMDB answers the same question in
    well under a second, because the source's slugs ARE TMDB ids (verified
    against live catalog pages).

    Returns None if TMDB is disabled or has nothing for this id, in which
    case the caller falls back to scraping - so this can only make the app
    faster, never less capable.
    """
    if not tmdb.is_enabled():
        return None

    data = await tmdb.get_title(slug, content_type)
    if not data:
        return None

    meta = tmdb.normalise_title(data, content_type)
    detail = TitleDetail(
        slug=slug,
        source=source,
        content_type=content_type,
        title=meta["title"],
        overview=meta["overview"],
        backdrop_url=meta["backdrop_url"],
        poster_url=meta["poster_url"],
        rating=meta["rating"],
        year=meta["year"],
        genres=meta["genres"],
        tagline=meta["tagline"],
        runtime=meta["runtime"],
        status=meta["status"],
        cast=[CastMember(**c) for c in meta["cast"]],
        trailer_key=meta["trailer_key"],
        similar=[SimilarTitle(**s) for s in meta["similar"]],
    )

    if content_type != "tv":
        return detail

    # Seasons come from the title payload; only the selected season's
    # episodes need a second call, mirroring how the UI actually browses.
    raw_seasons = [s for s in data.get("seasons", []) if isinstance(s, dict)]
    seasons = [
        Season(
            season_number=s.get("season_number", 0),
            name=s.get("name") or f"Season {s.get('season_number', 0)}",
            episode_count=s.get("episode_count") or 0,
            overview=s.get("overview") or "",
            poster_url=tmdb.image_url(s.get("poster_path")),
            air_date=s.get("air_date") or "",
        )
        for s in raw_seasons
        if (s.get("episode_count") or 0) > 0
    ]
    seasons.sort(key=lambda s: s.season_number)
    if not seasons:
        return None

    available = [s.season_number for s in seasons]
    # Skip "Specials" (season 0) by default - a viewer opening a series
    # almost always wants its first real season.
    if season is None or season not in available:
        season = next((n for n in available if n > 0), available[0])

    season_data = await tmdb.get_season(slug, season)
    if season_data:
        episodes = [
            Episode(**tmdb.normalise_episode(e, season))
            for e in season_data.get("episodes", [])
            if isinstance(e, dict)
        ]
        episodes.sort(key=lambda e: e.episode_number)
        for entry in seasons:
            if entry.season_number == season:
                entry.episodes = episodes
                if episodes:
                    entry.episode_count = max(entry.episode_count, len(episodes))

    detail.seasons = seasons
    detail.selected_season = season
    return detail


async def scrape_title_detail(
    slug: str,
    season: int | None = None,
    source: str = "vidbox",
    content_type: str = "tv",
) -> TitleDetail:
    """Return everything the detail page shows for one title.

    For a series this includes its seasons, with `season`'s episodes filled
    in; for a movie `seasons` is simply empty and only the hero metadata is
    scraped, which needs no browser driving at all (the title's TMDB object
    is inlined in the page's flight payload, so one render is enough).

    Only Vidbox is supported: detail structure is entirely source-specific,
    so other sources return an empty detail rather than pretending.
    """
    if source != "vidbox" or not settings.VIDBOX_BASE_URL:
        return TitleDetail(slug=slug, title="", source=source, content_type=content_type)

    # Try TMDB first - same data, ~20x faster, and it falls through to the
    # scrape below for anything it cannot answer.
    try:
        fast = await _detail_from_tmdb(slug, season, source, content_type)
        if fast is not None:
            return fast
    except Exception:  # noqa: BLE001 - never let the fast path break the slow one
        logger.warning("TMDB detail lookup failed for %s; scraping instead", slug, exc_info=True)

    path = "movie" if content_type == "movie" else "tv"
    # Pooled context: picks the next browser round-robin and applies its
    # persona (user agent, viewport, locale, timezone), so successive
    # scrapes do not all present as one identical client.
    context = await new_context()
    try:
        page = await context.new_page()
        detail_url = urljoin(settings.VIDBOX_BASE_URL, f"/{path}/{slug}")
        await page.goto(detail_url, timeout=settings.NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        raw, seasons = parse_detail(await page.content(), content_type)
        detail = _detail_from_payload(raw, slug, source, content_type, seasons)

        # A movie has no episodes to fetch, so the single render above is
        # the whole job - don't pay for driving the watch page.
        if content_type == "movie" or not seasons:
            if content_type != "movie":
                logger.warning("No seasons found for slug=%s", slug)
            return detail

        # Default to the first real season rather than season 0 ("Specials"),
        # which is what a viewer opening a series almost always wants.
        available = [s.season_number for s in seasons]
        if season is None or season not in available:
            season = next((n for n in available if n > 0), available[0])

        # Reuse the page already loaded above - _open_watch_page() starts
        # from the show page anyway, so re-navigating would waste a load.
        if await _open_watch_page(page, slug):
            await _select_season(page, season)
            episodes = await _parse_episode_cards(page, season)
            for entry in detail.seasons:
                if entry.season_number == season:
                    entry.episodes = episodes
                    # The rendered list is authoritative over the payload's
                    # count, which can disagree for a still-airing season.
                    if episodes:
                        entry.episode_count = max(entry.episode_count, len(episodes))
        else:
            # Seasons still go out, so the picker renders and the user can
            # pick an episode by number even though titles are missing.
            logger.warning("Returning seasons without episodes for slug=%s", slug)

        detail.selected_season = season
        return detail
    finally:
        await context.close()


# Previous name, kept so existing callers keep working. Series-only by
# signature, which is all it was ever used for.
async def scrape_series(slug: str, season: int | None = None, source: str = "vidbox") -> TitleDetail:
    """Deprecated alias for scrape_title_detail() with content_type="tv"."""
    return await scrape_title_detail(slug, season, source, "tv")
