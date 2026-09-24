"""
Pool of warm Playwright browsers with rotating personas.

Two problems this solves, which are really the same problem seen from two
sides:

  1. Throughput. Every scrape (catalog listing, search, stream capture)
     needs a browser. With a single Chromium, a stream capture and a
     catalog crawl queue behind each other on one process. A small pool
     lets them proceed in parallel.

  2. Fingerprint spread. A single browser means every request the app ever
     makes carries one identical user agent, viewport, locale and timezone.
     That is a stable, obvious signature. Each pool member instead gets its
     own persona from settings.BROWSER_* (see config.py), and contexts
     inherit the persona of the browser they were opened on - so the source
     sees traffic from several plausible-looking clients rather than one
     tireless robot.

Why a pool of BROWSERS rather than just more contexts on one browser:
contexts already isolate cookies and storage, but they share the parent
process - and much of what is fingerprintable (the actual Chromium build,
its process-level flags) comes from the process. Separate browsers give
separate ground truth for those.

Personas pair BY INDEX across the four config lists, so entry 0 of each
forms one coherent client. Mixing them independently would be worse than
not rotating at all: a Windows user agent reporting a Europe/London
timezone and a macOS platform is itself an anomaly.

Lifecycle: browsers launch lazily and stay warm for the process lifetime;
close_browser() is called from the FastAPI shutdown hook so no Chromium
lingers after the server exits.
"""
import asyncio
import itertools
import logging
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Persona:
    """One coherent client identity applied to a browser's contexts."""
    user_agent: str
    viewport: dict[str, int]
    locale: str
    timezone_id: str


def _parse_viewport(value: str) -> dict[str, int]:
    """Parse a "WIDTHxHEIGHT" config entry, falling back to 1920x1080."""
    try:
        width, height = value.lower().split("x", 1)
        return {"width": int(width), "height": int(height)}
    except (ValueError, AttributeError):
        logger.warning("Bad viewport %r in config; using 1920x1080", value)
        return {"width": 1920, "height": 1080}


def _build_personas() -> list[Persona]:
    """Assemble personas by pairing the config lists index-for-index.

    Falls back to the single legacy SPOOFED_USER_AGENT if the lists are
    empty, so a stripped-down config still works.
    """
    agents = list(settings.BROWSER_USER_AGENTS) or [settings.SPOOFED_USER_AGENT]
    viewports = list(settings.BROWSER_VIEWPORTS) or ["1920x1080"]
    locales = list(settings.BROWSER_LOCALES) or ["en-US"]
    zones = list(settings.BROWSER_TIMEZONES) or ["America/New_York"]

    personas: list[Persona] = []
    for i, agent in enumerate(agents):
        # Shorter lists wrap rather than truncating the persona count, so a
        # config listing 3 agents but 1 viewport still yields 3 personas.
        personas.append(
            Persona(
                user_agent=agent,
                viewport=_parse_viewport(viewports[i % len(viewports)]),
                locale=locales[i % len(locales)],
                timezone_id=zones[i % len(zones)],
            )
        )
    return personas


_playwright: Playwright | None = None
_browsers: list[Browser] = []
_personas: list[Persona] = []
_lock = asyncio.Lock()
# Round-robin cursor over the pool. itertools.count is unbounded, so this
# just increments; the modulo happens at use.
_cursor = itertools.count()


async def _ensure_pool() -> None:
    """Launch the pool if it is not already running.

    Re-checks inside the lock because several requests commonly arrive
    together on a cold start, and without that check each would launch its
    own set of browsers.
    """
    global _playwright, _personas

    if _browsers and all(b.is_connected() for b in _browsers):
        return

    async with _lock:
        if _browsers and all(b.is_connected() for b in _browsers):
            return

        # Drop any browser that died (crashed, or killed externally) so the
        # pool is rebuilt from a clean slate rather than holding a corpse.
        for dead in [b for b in _browsers if not b.is_connected()]:
            _browsers.remove(dead)

        if _playwright is None:
            _playwright = await async_playwright().start()

        _personas = _build_personas()
        size = max(1, settings.BROWSER_POOL_SIZE)

        while len(_browsers) < size:
            browser = await _playwright.chromium.launch(
                headless=settings.HEADLESS,
                args=[
                    # Reduces the most obvious automation tell. Not a
                    # complete disguise - nothing is - but it costs nothing
                    # and removes navigator.webdriver.
                    "--disable-blink-features=AutomationControlled",
                    # Memory trimming. Each browser spawns its own GPU,
                    # network and utility child processes, so a pool of
                    # three costs several GB unless the parts we never use
                    # are switched off. None of these affect scraping: the
                    # pages are parsed as HTML or watched for network
                    # requests, never painted for a human.
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-software-rasterizer",
                    "--disable-extensions",
                    "--no-sandbox",
                    "--mute-audio",
                    # Background tabs and renderer backgrounding matter here
                    # because a capture deliberately waits ~30s on a page;
                    # without these Chromium keeps timers and animations
                    # running on content nobody is looking at.
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
                ],
            )
            _browsers.append(browser)

        logger.info(
            "Browser pool ready: %d instance(s), %d persona(s)", len(_browsers), len(_personas)
        )


async def get_browser() -> Browser:
    """Return the next browser in the pool, round-robin.

    Kept for callers that manage their own context. Prefer new_context(),
    which also applies the matching persona - a browser alone carries none.
    """
    await _ensure_pool()
    return _browsers[next(_cursor) % len(_browsers)]


async def new_context(**overrides) -> BrowserContext:
    """Open a context on the next pooled browser, with its persona applied.

    This is the entry point scrapers should use: it is what actually makes
    requests look like they come from different clients, since the persona
    lives on the context (user agent, viewport, locale, timezone) rather
    than on the browser process.
    """
    await _ensure_pool()
    tick = next(_cursor)
    browser = _browsers[tick % len(_browsers)]
    # Cycle personas on their OWN counter rather than reusing the browser
    # index: with 2 browsers and 3 personas, index-sharing would pin
    # persona 0 to browser 0 and persona 1 to browser 1, leaving persona 2
    # permanently unused. Stepping them independently exercises every
    # configured persona regardless of pool size.
    persona = _personas[tick % len(_personas)] if _personas else None

    options: dict = {}
    if persona is not None:
        options.update(
            user_agent=persona.user_agent,
            viewport=persona.viewport,
            locale=persona.locale,
            timezone_id=persona.timezone_id,
        )
    options.update(overrides)

    try:
        return await browser.new_context(**options)
    except Exception:
        # A browser can die between the health check and this call. Rebuild
        # once and retry, rather than failing a user-visible request over a
        # recoverable pool problem.
        logger.warning("Context creation failed; rebuilding pool", exc_info=True)
        async with _lock:
            if browser in _browsers:
                _browsers.remove(browser)
        await _ensure_pool()
        fallback = _browsers[next(_cursor) % len(_browsers)]
        return await fallback.new_context(**options)


async def warm_up() -> None:
    """Launch the pool ahead of the first request.

    Called from the startup hook: a cold launch costs several seconds, and
    paying it at boot rather than on someone's first click is free.
    """
    try:
        await _ensure_pool()
    except Exception:  # noqa: BLE001 - startup must not fail on this
        logger.warning("Browser pool warm-up failed; will retry on demand", exc_info=True)


async def close_browser() -> None:
    """Close every pooled browser and stop Playwright. Call on shutdown."""
    global _playwright

    for browser in list(_browsers):
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 - already-dead browsers are fine
            pass
    _browsers.clear()

    if _playwright is not None:
        await _playwright.stop()
        _playwright = None
