"""
Central configuration for the scraper/proxy.

TARGET_BASE_URL is intentionally a placeholder. Point it (and, if the site
differs from the structure below, the selectors) at a site you own or are
explicitly authorized to scrape and embed. Every value here can be overridden
from the environment or a .env file with the MOVIEDIR_ prefix, e.g.:

    MOVIEDIR_TARGET_BASE_URL=https://the-real-site.example

so you never have to edit this file to swap targets.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Clerk auth ---
    # Secret key for Clerk's Backend API (user list/update/delete) - never
    # exposed to the frontend. Same value as frontend/.env.local's
    # CLERK_SECRET_KEY; duplicated here because Vite's .env.local isn't read
    # by this process.
    CLERK_SECRET_KEY: str = ""
    # Used to derive the JWKS URL for verifying session token signatures -
    # see app/clerk_auth.py. Same value as the frontend's
    # VITE_CLERK_PUBLISHABLE_KEY.
    CLERK_PUBLISHABLE_KEY: str = ""
    # Signing secret for the /api/webhooks/clerk endpoint (see
    # app/webhooks.py), issued when a webhook endpoint is created in the
    # Clerk dashboard. Verifies that an incoming user.created/updated/deleted
    # POST genuinely came from Clerk (Svix-signed) before it's trusted to
    # write to the users collection.
    CLERK_WEBHOOK_SIGNING_SECRET: str = ""

    # --- MongoDB ---
    # Connection string for the Atlas cluster. Empty by default so the app
    # still imports/runs with Mongo unconfigured; app/db.py only connects
    # when this is set.
    MONGODB_URI: str = ""
    MONGODB_DB_NAME: str = "movie_directory"

    # --- Target site ---
    # Placeholder: override via MOVIEDIR_TARGET_BASE_URL before running.
    TARGET_BASE_URL: str = "https://example-movies.test"

    # BingeFlix values belong in .env; empty defaults keep configuration
    # importable when the optional second source has not been configured.
    BINGEFLIX_BASE_URL: str = ""
    BINGEFLIX_EMBED_HOSTS: tuple[str, ...] = ()
    BINGEFLIX_EMBED_URL_PATTERNS: tuple[str, ...] = ()
    VIDBOX_BASE_URL: str = ""
    VIDBOX_EMBED_HOSTS: tuple[str, ...] = ()
    VIDBOX_EMBED_URL_PATTERNS: tuple[str, ...] = (".m3u8", ".mp4")

    # Catalog lives at the site root for this target; override if it doesn't.
    CATALOG_PATH: str = "/"
    # {slug} is the numeric content id (e.g. "1621552") captured from the
    # catalog's detail links (/details/{id}?m). This goes straight to the
    # watch/player page - the /details/ page itself is just a synopsis page
    # with a "Watch now" link into this URL, so there's no need to visit it.
    DETAIL_PATH_TEMPLATE: str = "/watch/{slug}?m&ep=1"
    # Substring in a catalog item's href that marks it as TV (not a movie) -
    # e.g. "/details/284825?tv" vs "/details/1621552?m". This is a movie
    # directory, so items matching this are filtered out at parse time
    # (DETAIL_PATH_TEMPLATE above assumes "?m" and would build the wrong
    # URL for a TV show). Leave empty to disable filtering.
    TV_SHOW_HREF_MARKER: str = "?tv"

    # Subtitle URL template - {slug} is the movie id, {lang} a language name
    # this site's subtitle CDN recognizes (currently only "English" has been
    # observed to exist for tested titles; others just 404, which the
    # subtitle endpoint treats as "no subtitles in that language" rather
    # than an error). Leave empty to disable subtitle lookup entirely.
    SUBTITLE_URL_TEMPLATE: str = "https://cache.vdrk.site/v1/vtt/movie/{slug}/{lang}.vtt"
    # TV subtitles live under a different path on the same CDN and are keyed
    # per episode, so a series cannot reuse the movie template - doing so
    # was why TV titles never showed subtitles at all. {slug} is the show
    # id, {season}/{episode} the episode. Leave empty to disable for TV.
    TV_SUBTITLE_URL_TEMPLATE: str = (
        "https://cache.vdrk.site/v1/vtt/tv/{slug}/{season}/{episode}/{lang}.vtt"
    )
    SUBTITLE_LANGUAGES: tuple[str, ...] = ("English",)

    # --- DOM selectors (exact structure of the target site) ---
    CATALOG_ITEM_SELECTOR: str = ".film_list-wrap .flw-item"
    LINK_SELECTOR: str = "a.film-poster-ahref"
    TITLE_SELECTOR: str = ".film-name a.dynamic-name"
    THUMBNAIL_SELECTOR: str = "img.film-poster-img"
    # The watch page's player loads automatically inside nested iframes
    # (cinetaro.to -> sub.php -> cinextream.cc/api/embed) - there is no
    # separate play button to click on this page, so this selector isn't
    # used for this site; kept for parity with the original architecture.
    PLAY_BUTTON_SELECTOR: str = "a.btn-play"

    # Substrings that mark a network request as the actual media stream to
    # capture in capture_embed_url(). Keep this narrow and specific to
    # "top-level" playable URLs - broader patterns like "/embed/" or
    # "/player/" match intermediate iframe hops (e.g.
    # cinetaro.to/src/player/sub.php) before the real .m3u8/.mp4 loads,
    # causing capture to resolve too early on the wrong URL. Segment files
    # (.ts) are deliberately excluded here even though they're real media -
    # they should never be the thing capture_embed_url() resolves on, since
    # the .m3u8 that references them is what the player actually needs.
    EMBED_URL_PATTERNS: tuple[str, ...] = (
        ".m3u8",
        ".mp4",
    )

    # Substrings that mark a URL as something the proxy may fetch on behalf
    # of an already-validated request - i.e. is_allowed_embed_url()'s pattern
    # check. Superset of EMBED_URL_PATTERNS: includes ".ts" so HLS segment
    # URIs rewritten out of a validated .m3u8 (see proxy.fetch_hls_manifest)
    # pass the same guardrail when the player fetches them individually.
    PROXY_ALLOWED_URL_PATTERNS: tuple[str, ...] = EMBED_URL_PATTERNS + (".ts",)

    # Extra hosts allowed through the proxy guardrail beyond TARGET_BASE_URL's
    # own host. Embeds are commonly served from a separate CDN/player host, so
    # add those here explicitly - the guardrail never wildcards.
    # Subdomains of a listed host are accepted; bare hostnames only, no scheme.
    ALLOWED_EMBED_HOSTS: tuple[str, ...] = ()

    # Same idea as ALLOWED_EMBED_HOSTS but for /api/proxy_image (poster
    # thumbnails), which is a separate, narrower allowlist since image CDNs
    # are usually a different host than the video/embed CDN.
    ALLOWED_IMAGE_HOSTS: tuple[str, ...] = ()

    # Headers used when impersonating a browser visiting the target site
    SPOOFED_USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # --- TMDB metadata ---
    # The source's slugs ARE TMDB ids (verified: 1396=Breaking Bad,
    # 108978=Reacher, 4614=NCIS), and the site itself just mirrors TMDB
    # payloads - so title/overview/rating/genres/seasons/episodes can come
    # straight from the API instead of costing a ~10s browser render.
    #
    # Metadata only. Which titles exist in the catalog, and the actual
    # stream URL, are still scraped - TMDB knows nothing about either.
    #
    # Leave BOTH empty to disable TMDB entirely and keep the current
    # scrape-everything behaviour; the scrape path remains the fallback for
    # any title TMDB cannot answer for, so nothing breaks either way.
    # Set exactly one: the v3 API key, or the v4 read token (a long "eyJ..."
    # bearer). If both are set, the v4 token wins.
    TMDB_API_KEY: str = ""
    TMDB_READ_TOKEN: str = ""
    TMDB_BASE_URL: str = "https://api.themoviedb.org/3"
    # ISO-639-1 language for titles/overviews TMDB returns.
    TMDB_LANGUAGE: str = "en-US"
    TMDB_TIMEOUT_S: float = 10.0
    # Cached TMDB responses (seconds). Metadata changes rarely, so this can
    # be far longer than anything tied to a stream token.
    TMDB_CACHE_TTL: int = 21_600  # 6 hours

    # --- Catalog index ---
    # The source offers only paginated listings - no genre, trending or
    # "everything" endpoint. To browse by genre while showing ONLY titles
    # the source actually has, its listings are crawled once and cached
    # here; categories are then computed over that set. See
    # app/catalog_index.py.
    #
    # Pages to crawl per content type. 20 titles per page, so the default
    # indexes roughly 500 movies + 500 shows. Raise for more coverage at
    # the cost of a longer initial crawl.
    CATALOG_INDEX_MAX_PAGES: int = 25
    # Titles per listing page, used to detect the end of a listing.
    CATALOG_PAGE_SIZE: int = 20
    # How many catalog pages to fetch concurrently while indexing. Should
    # not exceed BROWSER_POOL_SIZE by much - beyond that the extra tasks
    # just queue on the same browsers while adding load at the source.
    CATALOG_INDEX_CONCURRENCY: int = 3
    # Pause between page batches while crawling. Deliberately not zero:
    # hammering the source is what gets a scraper blocked.
    CATALOG_INDEX_DELAY_S: float = 1.0
    # How long an index stays fresh before a full rebuild is allowed
    # (seconds). Incremental refreshes (see CATALOG_INDEX_REFRESH_INTERVAL_S)
    # run far more often than this - this TTL only gates the expensive
    # every-page crawl used as a periodic safety net.
    CATALOG_INDEX_TTL: int = 86_400  # 24 hours
    # Start crawling automatically when the server starts.
    CATALOG_INDEX_ON_STARTUP: bool = True
    # How often the background scheduler runs an incremental refresh
    # (see catalog_index.refresh_index). Cheap - it stops as soon as a page
    # yields no new titles - so this can be fairly frequent.
    CATALOG_INDEX_REFRESH_INTERVAL_S: int = 3_600  # 1 hour
    # A title indexed more recently than this counts as "recently added"
    # for sort purposes (see catalog_index._popularity_score / app/main.py).
    CATALOG_INDEX_RECENT_WINDOW_S: int = 14 * 86_400  # 14 days

    # How long a probed subtitle-availability result (hit or miss) stays
    # cached before being re-checked (see app/main.py's get_subtitles). Long
    # because availability essentially never changes once set - this is
    # mainly a safety net against a permanent false negative from a
    # transient CDN hiccup during the original probe.
    SUBTITLE_PROBE_TTL: int = 30 * 86_400  # 30 days

    # --- Browser pool ---
    # How many separate Chromium instances to rotate captures across.
    # Two reasons for more than one:
    #   1. Throughput - a catalog scrape and a stream capture no longer
    #      queue behind each other on a single browser process.
    #   2. Fingerprint spread - each instance gets its own persona from the
    #      lists below (user agent, viewport, locale, timezone), so the
    #      source does not see every request arriving as one identical
    #      client. Raise cautiously: each instance is a real Chromium
    #      process and costs memory.
    BROWSER_POOL_SIZE: int = 2
    # Personas assigned round-robin to pool members. Keep these mutually
    # consistent - a Windows user agent paired with a macOS platform is
    # itself a fingerprinting signal, so change them as a set.
    BROWSER_USER_AGENTS: tuple[str, ...] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    # "WIDTHxHEIGHT" entries, paired with the user agents by index.
    BROWSER_VIEWPORTS: tuple[str, ...] = ("1920x1080", "1680x1050", "1536x864")
    BROWSER_LOCALES: tuple[str, ...] = ("en-US", "en-GB", "en-US")
    BROWSER_TIMEZONES: tuple[str, ...] = (
        "America/New_York",
        "Europe/London",
        "America/Los_Angeles",
    )

    # Hosts to block outright during embed capture (page.route abort) -
    # ad/tracking networks observed firing on the watch page. These never
    # contribute to finding the real stream URL and slow capture down by
    # loading redundant scripts/pixels/redirects; blocking them also means
    # no ad content ever has a chance to load in the first place, since
    # capture_embed_url() only ever extracts the one .m3u8/.mp4 URL and the
    # ad network's own resources are never fetched again after that (the
    # proxy only re-fetches the URL the frontend asks for). Add more here as
    # you spot them in server logs / browser devtools; this is a blocklist,
    # not an allowlist, so an unrecognized new domain fails open (loads) -
    # slower but not broken.
    BLOCKED_AD_HOSTS: tuple[str, ...] = (
        "255md.com",
        "al5sm.com",
        "my.rtmark.net",
        "protrafficinspector.com",
        "downyattainprojects.com",
        "mamshirt.com",
        "portalfluently.com",
        "realizationnewestfangs.com",
        "www.google-analytics.com",
        "www.googletagmanager.com",
        "static.cloudflareinsights.com",
        "kettledroopingcontinuation.com",
        "interlinecustomroofingllc.com",
        "s10.histats.com",
        "s4.histats.com",
        "t.dtscout.com",
        "e.dtscout.com",
        "t.dtscdn.com",
        "tags.crwdcntrl.net",
        "pixel.onaudience.com",
        "p.mrktmtrcs.net",
        "aniserver.cinetaro.tv",
    )
    # Resource types to block during embed capture regardless of host - none
    # of these can ever be the stream URL itself, and skipping them (fonts,
    # full-res images, stylesheets) measurably speeds up reaching the real
    # player request. "document" and "media"/"xhr"/"fetch" are deliberately
    # excluded since one of those IS the thing we're waiting for.
    BLOCKED_RESOURCE_TYPES: tuple[str, ...] = ("image", "font", "stylesheet")

    # Playwright behavior
    NAV_TIMEOUT_MS: int = 20_000
    SELECTOR_TIMEOUT_MS: int = 15_000
    # Some players try a primary stream source, see it fail (e.g. an expired
    # CDN link), and fall back to a different backend several seconds later -
    # capture only resolves on a successful (2xx) response (see
    # scraper_embed.py), so this needs enough headroom to cover that retry,
    # not just the first attempt.
    # Budget for the whole capture: the Play-click retries AND the wait for
    # the stream request now share this window (see scraper_embed.py), so it
    # needs headroom for both. At the old 30s a title whose first click did
    # not take could spend the entire budget clicking and time out with no
    # stream, even though the stream was moments away.
    EMBED_CAPTURE_TIMEOUT_MS: int = 45_000
    HEADLESS: bool = True

    # Vidbox mounts its player only once the Play button's React handler has
    # attached, which happens well after domcontentloaded. Its page also
    # embeds an autoplaying trailer that keeps the network permanently busy,
    # so there is no networkidle/hydration signal to wait on - the scraper
    # instead clicks up to this many times, waiting this long after each for
    # the player iframe to replace the trailer.
    VIDBOX_PLAY_CLICK_ATTEMPTS: int = 6
    VIDBOX_PLAYER_MOUNT_TIMEOUT_MS: int = 5_000

    # Simple in-memory cache TTL (seconds) for catalog scrape results
    CATALOG_CACHE_TTL: int = 300
    # TTL (seconds) for the PERSISTED /api/detail response (series_details
    # Mongo collection) - deliberately much longer than CATALOG_CACHE_TTL,
    # which also gates the in-process series/catalog caches. Those are cheap
    # to let expire quickly since refilling them just re-reads Mongo or
    # replays an already-cheap TMDB call; this one gates the EXPENSIVE
    # rebuild path (a ~10s+ browser scrape for episodes when TMDB has
    # nothing, or a fresh TMDB round-trip otherwise) surviving a restart or
    # another worker, so it should track how often a title's metadata (cast,
    # trailer, seasons, episodes) actually changes - rarely - rather than how
    # often a live scrape cache should be trusted.
    SERIES_DETAIL_CACHE_TTL: int = 21_600  # 6 hours

    class Config:
        env_prefix = "MOVIEDIR_"
        env_file = ".env"


settings = Settings()
