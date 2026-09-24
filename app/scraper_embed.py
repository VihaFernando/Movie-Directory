"""
Network interception to capture the embed/stream URL.

Flow:
  1. Build the detail URL from DETAIL_PATH_TEMPLATE + slug.
  2. Arm listeners BEFORE navigating - interception has to be in place first
     or an early request fires and is missed.
  3. Race waiting for PLAY_BUTTON_SELECTOR against waiting for the stream
     URL itself - whichever happens first wins. Some watch pages auto-play
     with no button at all, in which case the stream URL typically arrives
     well before SELECTOR_TIMEOUT_MS would otherwise elapse; waiting for the
     button sequentially in that case would needlessly burn the whole
     timeout doing nothing. If the button does appear first, click it, then
     keep waiting for the stream URL.
  4. Resolve on the first *successful* (2xx) response whose URL looks like
     an embed/stream, seen either as a completed network response or as a
     newly attached iframe's URL. Resolving on the request firing (rather
     than its response) would lock onto a dead/expired CDN link before its
     player's own fallback to a working mirror ever gets a chance to fire -
     some sites' players try a primary source, see it fail, and retry a
     different backend a few seconds later; only the response outcome tells
     the two apart.

Both signals matter: some players fetch the stream directly (request), others
inject an <iframe src="..."> whose document load is what we can observe.

Uses the shared browser from app.browser_pool (one warm Chromium instance,
a fresh BrowserContext per call) instead of launching a new browser process
per request - see browser_pool.py for why. Ad/tracking requests are blocked
during capture via page.route() (see settings.BLOCKED_AD_HOSTS /
BLOCKED_RESOURCE_TYPES) so capture reaches the real stream request faster
and no ad content is ever fetched in the process.
"""
import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse

from playwright.async_api import Frame, Request, Response, Route

from app.browser_pool import new_context
from app.config import settings
from app.proxy import record_captured_url

logger = logging.getLogger(__name__)

# Hosts Vidbox mounts its real player iframe from, once the Play button's
# handler runs. Matching any of them means the trailer has been replaced by
# the player and the stream request is imminent. Derived from the configured
# embed hosts so adding a newly-observed host to .env is enough.
_VIDBOX_PLAYER_IFRAME_SELECTOR = ",".join(
    f"iframe[src*='{host}']" for host in settings.VIDBOX_EMBED_HOSTS
) or "iframe[src]"


def _looks_like_embed(url: str, patterns: tuple[str, ...]) -> bool:
    """True if `url` matches one of the configured embed/stream patterns."""
    if not url or url.startswith(("about:", "data:", "blob:")):
        return False
    return any(pattern in url for pattern in patterns)


# Resource types safe to abort during a STREAM CAPTURE. Deliberately
# narrower than settings.BLOCKED_RESOURCE_TYPES (which the catalog scraper
# uses): "stylesheet" is absent because aborting CSS prevents Vidbox's
# player from mounting at all, so the capture finds no stream to intercept.
# Images and fonts are safe - they affect appearance, never player state.
_CAPTURE_BLOCKED_RESOURCE_TYPES = ("image", "font")


def _is_blocked(route_url: str, resource_type: str) -> bool:
    """True if a request should be aborted during capture - a known
    ad/tracking host, or a resource type that cannot affect playback.

    See _CAPTURE_BLOCKED_RESOURCE_TYPES for why this is narrower than the
    catalog scraper's blocklist.
    """
    if resource_type in _CAPTURE_BLOCKED_RESOURCE_TYPES:
        return True
    host = urlparse(route_url).hostname or ""
    return any(host == blocked or host.endswith("." + blocked) for blocked in settings.BLOCKED_AD_HOSTS)


def build_detail_url(
    slug: str,
    source: str = "current",
    content_type: str = "movie",
    season: int | None = None,
    episode: int | None = None,
) -> str:
    """Construct the detail page URL for a title.

    For TV, `season`/`episode` select which episode to resolve. BingeFlix
    takes them as query parameters directly; Vidbox cannot (its watch route
    is client-side state, not a URL - see scraper_series._open_watch_page),
    so there the episode is chosen by driving the page instead and this
    just returns the show page. Both default to S1E1 when unspecified,
    which is what the whole TV path used to be hardcoded to.
    """
    if source == "bingeflix":
        path = "movie" if content_type == "movie" else "tv"
        if content_type == "movie":
            suffix = ""
        else:
            suffix = f"?season={season or 1}&episode={episode or 1}"
        return urljoin(settings.BINGEFLIX_BASE_URL, f"/{path}/{slug}{suffix}")
    if source == "vidbox":
        path = "movie" if content_type == "movie" else "tv"
        return urljoin(settings.VIDBOX_BASE_URL, f"/{path}/{slug}")
    return urljoin(settings.TARGET_BASE_URL, settings.DETAIL_PATH_TEMPLATE.format(slug=slug))


async def _select_vidbox_episode(page, season: int | None, episode: int | None) -> bool:
    """On a Vidbox TV watch page, switch to `season` and start `episode`.

    Vidbox has no addressable per-episode URL - /watch/tv is reached by
    client-side routing and 404s on a direct GET - so the only way to play
    a specific episode is to drive the page: pick the season in its
    combobox, then click that episode's card, which kicks off a fresh
    stream request for it.

    Returns True if an episode card was actually clicked. False means the
    page stayed on whatever episode it opened with (its first), which is
    still playable - the caller just cannot claim it honoured the request.
    """
    if season is None and episode is None:
        return False

    target_season = season or 1
    target_episode = episode or 1

    try:
        combobox = page.get_by_role("combobox").first
        await combobox.wait_for(timeout=settings.SELECTOR_TIMEOUT_MS)
        current = (await combobox.inner_text()).strip()
        # Skip the menu entirely when the wanted season is already shown -
        # an open dropdown overlays the episode list we are about to click.
        if not re.fullmatch(rf"Season\s*{target_season}", current, re.I):
            await combobox.click()
            option = (
                page.get_by_role("option")
                .filter(has_text=re.compile(rf"^\s*Season\s*{target_season}\s*$", re.I))
                .first
            )
            await option.wait_for(timeout=settings.SELECTOR_TIMEOUT_MS)
            await option.click()
            # Re-rendered from data already in the page, so there is no
            # request to wait on - just let the list settle.
            await page.wait_for_timeout(1200)
    except Exception:  # noqa: BLE001 - fall through on the season shown
        logger.warning("Could not select season %s for slug", target_season)

    try:
        heading = page.locator("h1", has_text=re.compile(r"^\s*Episodes\s*$", re.I)).first
        await heading.wait_for(timeout=settings.SELECTOR_TIMEOUT_MS)
        cards = heading.locator("xpath=../..").locator("a:has(h2)")
        count = await cards.count()
        if not count:
            return False
        # Episode N is at index N-1 for a normally-numbered season; clamp
        # rather than fail so an out-of-range request still plays something.
        index = min(max(target_episode - 1, 0), count - 1)
        await cards.nth(index).click(force=True)
        logger.info("Selected S%sE%s on the watch page", target_season, target_episode)
        return True
    except Exception:  # noqa: BLE001 - capture still races on the default episode
        logger.warning("Could not click episode %s of season %s", target_episode, target_season)
        return False


async def capture_embed_url(
    slug: str,
    source: str = "current",
    content_type: str = "movie",
    season: int | None = None,
    episode: int | None = None,
) -> str | None:
    """Open the title's detail page, click play, and return the first
    intercepted embed/stream URL - or None if nothing matched in time.

    For TV, `season`/`episode` pick which episode is resolved. On Vidbox
    that cannot be done by URL (the watch route is client-side state), so
    the episode is selected by driving the watch page's season picker and
    episode list - see _select_vidbox_episode(). Omitting them keeps the
    previous behaviour of resolving whatever the page opens on, which for
    a series is its first episode.

    Scope worth knowing: this drives whichever server the watch page selects
    BY DEFAULT. Vidbox in particular also exposes a "Select a server" menu
    with a dozen-odd alternatives, and this function never touches those.

    So a None here means "the default server yielded no stream", NOT "this
    title has no video anywhere". Titles do exist whose default server
    reports "This media is unavailable" while the title plays fine on the
    source site via another server. Don't let a caller's error message claim
    more than that - it did once, and it was wrong.
    """
    detail_url = build_detail_url(slug, source, content_type, season, episode)
    if source == "bingeflix":
        patterns = settings.BINGEFLIX_EMBED_URL_PATTERNS
    elif source == "vidbox":
        patterns = settings.VIDBOX_EMBED_URL_PATTERNS
    else:
        patterns = settings.EMBED_URL_PATTERNS
    # A one-element list rather than a bare future because the TV path
    # swaps in a FRESH future partway through (see the episode-selection
    # block below): the page starts resolving its default episode before we
    # can pick the requested one, so that first capture has to be discarded.
    # The listeners below run for the whole page lifetime and must always
    # target the current future, so they read it through this holder rather
    # than closing over one particular future object.
    captured_ref: list[asyncio.Future[str]] = [asyncio.get_running_loop().create_future()]

    def _resolve(url: str, source: str) -> None:
        captured = captured_ref[0]
        if not captured.done() and _looks_like_embed(url, patterns):
            logger.info("Captured embed URL via %s: %s", source, url)
            # Register before resolving: the frontend will immediately ask
            # /api/proxy_embed for this URL, and providers that rotate the
            # stream host per title (Vidbox) can't be covered by the static
            # allowlist. See proxy._discovered_urls.
            record_captured_url(url)
            captured.set_result(url)

    def on_response(response: Response) -> None:
        # Only resolve on a response that actually succeeded - a request
        # alone doesn't mean the URL is usable, and resolving on a dead/
        # expired CDN link would pre-empt the player's own retry against a
        # working mirror (see module docstring).
        if response.ok:
            _resolve(response.url, "response")
        else:
            logger.debug("Non-OK response (%s) for %s - not treating as capture", response.status, response.url)

    def on_request(request: Request) -> None:
        # Vidbox rotates a tokenized manifest inside a nested provider frame;
        # capture the request immediately because the response may complete
        # while the frame is being replaced by the player.
        if source == "vidbox" and _looks_like_embed(request.url, patterns):
            _resolve(request.url, "request")

    def on_frame_attached(frame: Frame) -> None:
        # A frame's URL is often empty at attach time; re-check once it navigates.
        _resolve(frame.url, "frame")

    def on_frame_navigated(frame: Frame) -> None:
        _resolve(frame.url, "frame-navigated")

    async def route_handler(route: Route) -> None:
        if _is_blocked(route.request.url, route.request.resource_type):
            await route.abort()
        else:
            await route.continue_()

    # Pooled context: picks the next browser round-robin and applies its
    # persona (user agent, viewport, locale, timezone), so successive
    # scrapes do not all present as one identical client.
    context = await new_context()
    try:
        page = await context.new_page()

        # Arm every listener/route before navigation so nothing is missed.
        page.on("response", on_response)
        page.on("request", on_request)
        page.on("frameattached", on_frame_attached)
        page.on("framenavigated", on_frame_navigated)
        # Block ads and trackers, but NOT stylesheets, on the capture path.
        #
        # This is a hard-won distinction. Blocking stylesheets here breaks
        # Vidbox outright: with them aborted the player never mounts and the
        # capture times out with no stream at all (measured: 12.9s and a
        # working stream with styles allowed, versus 70.6s and zero media
        # with them blocked). Its player is laid out by CSS and evidently
        # gates mounting on that, so "stylesheets cannot be the stream" -
        # true as far as it goes - was the wrong test. What matters is
        # whether the page still reaches the state that PRODUCES a stream.
        #
        # Ad hosts stay blocked: they genuinely never contribute, and
        # skipping them still speeds the capture up. The catalog scraper is
        # unaffected and keeps blocking images/fonts/styles, since it only
        # ever parses HTML and never needs a rendered player.
        await page.route("**/*", route_handler)

        await page.goto(detail_url, timeout=settings.NAV_TIMEOUT_MS, wait_until="domcontentloaded")

        if source == "vidbox":
            # Match the button whose label is exactly "Play" - has_text= is a
            # substring match, so a bare "Play" would also select "Replay" /
            # "Play trailer" style controls. Take the first match rather than
            # a fixed nth(): the page renders a single Play control, and its
            # index among all <button>s shifts with unrelated chrome (nav,
            # per-card watchlist buttons), so a positional guess silently
            # times out whenever that chrome changes.
            try:
                play_button = page.get_by_role(
                    "button", name=re.compile(r"^\s*play\s*$", re.I)
                ).first
                await play_button.wait_for(timeout=settings.SELECTOR_TIMEOUT_MS)
                # The button renders before its React handler is attached, so
                # the first click is frequently a no-op that leaves the page
                # sitting on its trailer. There is no reliable "hydrated"
                # signal to wait on here - the trailer iframe keeps the
                # network busy indefinitely, so networkidle never fires -
                # so click until the real player iframe actually swaps in.
                # Clicking again once it has mounted is harmless.
                #
                # The click budget is bounded by the CAPTURE deadline, not
                # just by the attempt count. Previously this loop could run
                # 6 attempts x 5s = 30s, which is the entire capture timeout
                # - so on a title where the first click did not take, the
                # clicking itself consumed the whole budget and the capture
                # timed out even though the stream would have arrived. That
                # produced spurious "no stream" failures on titles that play
                # perfectly well on the source.
                deadline = (
                    asyncio.get_running_loop().time()
                    + settings.EMBED_CAPTURE_TIMEOUT_MS / 1000
                )
                for _ in range(settings.VIDBOX_PLAY_CLICK_ATTEMPTS):
                    # Stop clicking once the stream has already been seen -
                    # further clicks can only disturb a working player.
                    if captured_ref[0].done():
                        break
                    remaining = deadline - asyncio.get_running_loop().time()
                    # Leave the tail of the budget for the capture wait
                    # below; clicking right up to the deadline guarantees a
                    # timeout instead of a result.
                    if remaining <= settings.VIDBOX_PLAYER_MOUNT_TIMEOUT_MS / 1000:
                        break
                    # Once the page has routed to its watch URL the Play
                    # button is gone, and clicking again is not just useless
                    # but actively harmful: Playwright waits out its FULL
                    # default timeout (30s) on the detached element, which
                    # burned the entire capture budget on titles that were
                    # about to produce a stream. Stop as soon as we have
                    # arrived.
                    if "/watch/" in page.url:
                        break
                    try:
                        # Cap the click itself too. The deadline check above
                        # cannot help once a click is already hanging, so the
                        # per-click timeout is what actually bounds this.
                        await play_button.click(
                            force=True, timeout=settings.VIDBOX_PLAYER_MOUNT_TIMEOUT_MS
                        )
                    except Exception:  # noqa: BLE001 - button can detach mid-route-change
                        # A detached button means the route already changed;
                        # nothing left to click.
                        if "/watch/" in page.url:
                            break
                    try:
                        await page.wait_for_selector(
                            _VIDBOX_PLAYER_IFRAME_SELECTOR,
                            timeout=settings.VIDBOX_PLAYER_MOUNT_TIMEOUT_MS,
                        )
                        break
                    except Exception:  # noqa: BLE001 - retry; capture still races below
                        continue

                # For a specific episode, the watch page has already begun
                # resolving its DEFAULT episode (S1E1) by this point, so
                # whatever `captured` holds now is the wrong stream. Reset
                # it before driving the picker, so the capture that
                # ultimately resolves is the one the episode click triggers
                # rather than the pilot's.
                wants_episode = content_type == "tv" and (season is not None or episode is not None)
                if wants_episode:
                    captured_ref[0] = asyncio.get_running_loop().create_future()
                    if not await _select_vidbox_episode(page, season, episode):
                        logger.warning(
                            "Falling back to the default episode for slug=%s (S%sE%s not selectable)",
                            slug, season, episode,
                        )

                try:
                    return await asyncio.wait_for(
                        captured_ref[0],
                        timeout=settings.EMBED_CAPTURE_TIMEOUT_MS / 1000,
                    )
                except asyncio.TimeoutError:
                    logger.warning("No Vidbox media URL captured for slug=%s", slug)
                    return None
            except Exception:  # noqa: BLE001 - capture below reports a missing player
                logger.warning("Vidbox Play button was not available for %s", detail_url)

        # Some players publish the playable iframe URL in the DOM, then fail
        # its navigation in headless Chromium because of their own embed
        # restrictions. The URL is still the correct handoff for our local
        # iframe proxy, so inspect it before waiting for network events.
        if source == "bingeflix":
            try:
                await page.wait_for_selector("iframe", timeout=5_000)
            except Exception:  # noqa: BLE001 - the regular capture race remains the fallback
                pass
        for iframe in await page.locator("iframe").all():
            iframe_url = await iframe.get_attribute("src")
            _resolve(iframe_url or "", "iframe-src")

        # Race the play-button wait against the capture itself, rather than
        # waiting out the full SELECTOR_TIMEOUT_MS before ever checking
        # whether the stream URL already arrived: on sites whose watch page
        # auto-plays with no button (no click needed - see module docstring),
        # a sequential wait_for_selector() call burns its entire timeout
        # doing nothing, even though `captured` resolved seconds earlier.
        selector_wait = asyncio.ensure_future(
            page.wait_for_selector(settings.PLAY_BUTTON_SELECTOR, timeout=settings.SELECTOR_TIMEOUT_MS)
        )
        capture_wait = asyncio.ensure_future(
            asyncio.wait_for(captured_ref[0], timeout=settings.EMBED_CAPTURE_TIMEOUT_MS / 1000)
        )
        done, pending = await asyncio.wait(
            {selector_wait, capture_wait}, return_when=asyncio.FIRST_COMPLETED
        )

        if capture_wait in done and not capture_wait.exception():
            # Stream URL already captured (auto-play, or the click wasn't
            # even needed) - no reason to wait for/click the button at all.
            selector_wait.cancel()
            return capture_wait.result()

        if selector_wait in done and not selector_wait.exception():
            # Button appeared before the stream fired - click it, then wait
            # (for whatever's left of) the capture timeout.
            try:
                # The play control is frequently overlaid by a poster/consent
                # layer, so click through rather than failing on interception.
                await page.click(settings.PLAY_BUTTON_SELECTOR, force=True)
                logger.info("Clicked play button %r on %s", settings.PLAY_BUTTON_SELECTOR, detail_url)
            except Exception:  # noqa: BLE001 - element may have vanished between wait and click
                logger.warning("Play button %r appeared but click failed on %s", settings.PLAY_BUTTON_SELECTOR, detail_url)
            try:
                return await capture_wait
            except asyncio.TimeoutError:
                logger.warning("No embed request captured for slug=%s", slug)
                return None

        # selector_wait failed (button never appeared - likely auto-play with
        # no button on this site) - fall back to waiting out capture_wait.
        selector_wait.cancel()
        try:
            return await capture_wait
        except asyncio.TimeoutError:
            logger.warning("No embed request captured for slug=%s", slug)
            return None
    finally:
        # Detach the future from any late callback firing during teardown.
        if not captured_ref[0].done():
            captured_ref[0].cancel()
        # Close only this context - the shared browser process stays warm
        # for the next request.
        await context.close()
