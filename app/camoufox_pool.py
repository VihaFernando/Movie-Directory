"""
A separate, single-purpose Camoufox launcher used ONLY by embed capture
(app.scraper_embed.capture_embed_url).

Why a second browser pool rather than reusing app.browser_pool:

Catalog scraping (app.scraper_catalog) already works fine against the
hosted deployment's IP - Chromium there gets a plain HTML listing page with
no anti-bot challenge. Embed capture is the one path that hits Cloudflare's
"Verify you are human" managed challenge on that same IP (confirmed via a
debug screenshot - see app/scraper_embed.py's _dump_debug_snapshot), which a
real Chromium automation build cannot pass, challenge or no challenge,
because part of what it's scored on is IP reputation, not just browser
fingerprint.

Camoufox is a separately-patched Firefox build (not a Playwright launch
flag) specifically aimed at defeating headless-browser fingerprinting -
independent, community-benchmarked results put its headless detection rate
at 0% against standard tests, better than stealth-patched Chromium. It is
NOT guaranteed to fix an IP-reputation-based block (a different, separate
signal Cloudflare scores independently of the browser), so this is a real
experiment, not a known fix.

Deliberately NOT using the `geoip`/`[geoip]` extra here (unlike an earlier
version of this file): it adds ~45MB of MaxMind database downloads to the
Docker build with no bearing on whether this fixes the actual IP-reputation
block, and a heavier build was the likely cause of a prior HF Space getting
stuck in a broken/paused platform state after a build that size. Geoip's
only effect is picking a plausible locale/timezone/screen for the
fingerprint; the timezone the OS clock already reports is an acceptable
default without it.

Kept deliberately separate from browser_pool.py: if this turns out not to
help, or makes captures slower/flakier, it can be ripped out without any
risk to catalog scraping, which already works.
"""
import logging

from camoufox.async_api import AsyncNewBrowser
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from app.config import settings

logger = logging.getLogger(__name__)

_playwright: Playwright | None = None
_browser: Browser | None = None


async def _ensure_browser() -> Browser:
    """Launch the single shared Camoufox browser if not already running.

    Just one instance, not a pool: Camoufox's own fingerprint generation
    already randomizes OS/screen/fonts/etc. per launch (see
    camoufox.utils.launch_options), so a single long-lived browser with a
    fresh BrowserContext per capture gets fingerprint variety for free,
    the same way app.browser_pool's persona rotation does manually for
    Chromium. A pool of several Camoufox instances could be added later if
    capture throughput ever becomes the bottleneck - it isn't yet.
    """
    global _playwright, _browser

    if _browser is not None and _browser.is_connected():
        return _browser

    if _playwright is None:
        _playwright = await async_playwright().start()

    _browser = await AsyncNewBrowser(
        _playwright,
        headless=settings.HEADLESS,
        humanize=True,  # randomized, human-like mouse movement on interactions
        i_know_what_im_doing=True,  # suppress Camoufox's "don't use headless" warning
    )
    logger.info("Camoufox browser launched for embed capture")
    return _browser


async def new_capture_context(**overrides) -> BrowserContext:
    """Open a fresh context on the shared Camoufox browser for one capture.

    A new context per call (not a new browser) mirrors app.browser_pool's
    new_context(): cheap isolation of cookies/storage per request without
    paying Camoufox's full launch cost again.
    """
    browser = await _ensure_browser()
    return await browser.new_context(**overrides)


async def close_camoufox() -> None:
    """Close the shared Camoufox browser. Call on FastAPI shutdown, mirroring
    app.browser_pool.close_browser()."""
    global _playwright, _browser

    if _browser is not None:
        try:
            await _browser.close()
        except Exception:  # noqa: BLE001 - already-dead browser is fine
            pass
        _browser = None

    if _playwright is not None:
        await _playwright.stop()
        _playwright = None
