"""
Dynamic catalog scraping.

Playwright renders the JS-driven catalog page and waits for
CATALOG_ITEM_SELECTOR (".list-movie .col") to appear; BeautifulSoup then
parses the fully-rendered HTML. Each tool does what it's good at - the
browser handles JS, the parser handles extraction.

Per-item structure assumed:
    .list-movie .col          <- CATALOG_ITEM_SELECTOR (one card)
      a.poster[href]          <- LINK_SELECTOR       (detail page link)
      .item-title             <- TITLE_SELECTOR      (title text)
      picture img[src]        <- THUMBNAIL_SELECTOR  (poster image)
"""
import logging
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser_pool import new_context
from app.config import settings
from app.models import Movie

logger = logging.getLogger(__name__)

# Lazy-loading galleries park the real image in a data-* attribute and leave
# src as a tiny placeholder (often a data: URI blur-up) until scroll/JS swaps
# it in - so check the data-* attrs first, src last.
_IMG_SRC_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-srcset", "src")


async def fetch_rendered_html(url: str, wait_selector: str) -> str:
    """Navigate to `url` headless and return the HTML once `wait_selector`
    has rendered (i.e. client-side JS has populated the grid).

    Uses the shared browser from app.browser_pool rather than launching a
    fresh Chromium process per call - see browser_pool.py.
    """
    # Pooled context: picks the next browser round-robin and applies its
    # persona (user agent, viewport, locale, timezone), so successive
    # scrapes do not all present as one identical client.
    context = await new_context()
    try:
        page = await context.new_page()

        # The catalog page is parsed as HTML - its poster images, fonts and
        # stylesheets are never read. Aborting them makes each page render
        # faster and, across a 37-page crawl, avoids downloading hundreds of
        # full-size images purely to throw them away.
        async def _block(route):
            request = route.request
            host = urlparse(request.url).hostname or ""
            blocked_host = any(
                host == entry or host.endswith("." + entry)
                for entry in settings.BLOCKED_AD_HOSTS
            )
            if request.resource_type in settings.BLOCKED_RESOURCE_TYPES or blocked_host:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _block)
        await page.goto(url, timeout=settings.NAV_TIMEOUT_MS, wait_until="domcontentloaded")

        try:
            await page.wait_for_selector(wait_selector, timeout=settings.SELECTOR_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            # Return whatever rendered rather than failing outright; the
            # parser will simply find no cards and the caller reports that.
            logger.warning("Selector %r did not appear within timeout on %s", wait_selector, url)

        return await page.content()
    finally:
        await context.close()


def _extract_image_src(card) -> str | None:
    """Pull the poster URL out of `picture img`, tolerating lazy-load attrs."""
    img = card.select_one(settings.THUMBNAIL_SELECTOR)
    if img is None:
        return None
    for attr in _IMG_SRC_ATTRS:
        value = img.get(attr)
        if value and not value.startswith("data:"):
            # srcset-style values are "url1 1x, url2 2x" - take the first URL.
            return value.split(",")[0].strip().split(" ")[0]
    return None


def _slug_from_href(href: str, base_url: str) -> str:
    """Derive the movie slug from a detail href.

    Handles both "/movie/some-slug/" and fully-qualified URLs, and tolerates
    query strings and trailing slashes.
    """
    path = urlparse(urljoin(base_url, href)).path
    segments = [segment for segment in path.split("/") if segment]
    return segments[-1] if segments else ""


def _is_tv_show_href(href: str) -> bool:
    """True if `href` matches settings.TV_SHOW_HREF_MARKER, marking it as a
    TV show rather than a movie. See TV_SHOW_HREF_MARKER for why these are
    filtered out."""
    marker = settings.TV_SHOW_HREF_MARKER
    return bool(marker) and marker in href


def parse_catalog(html: str, base_url: str, source: str = "current", content_type: str = "movie") -> list[Movie]:
    """Parse rendered catalog HTML into Movie objects using the configured selectors."""
    soup = BeautifulSoup(html, "html.parser")
    movies: list[Movie] = []
    seen: set[str] = set()

    if source == "bingeflix":
        cards = soup.select("article.poster-card")
    elif source == "vidbox":
        cards = soup.select('a[href^="/movie/"], a[href^="/tv/"]')
    else:
        cards = soup.select(settings.CATALOG_ITEM_SELECTOR)
    logger.info("Found %d catalog items matching %r", len(cards), settings.CATALOG_ITEM_SELECTOR)

    for card in cards:
        if source == "bingeflix":
            link_el = card.select_one("a.poster-watch")
            title_el = card.select_one(".poster-title")
            image_el = card.select_one("img")
        elif source == "vidbox":
            link_el = card
            image_el = card.select_one("img")
            title_el = image_el
        else:
            link_el = card.select_one(settings.LINK_SELECTOR)
            title_el = card.select_one(settings.TITLE_SELECTOR)
            image_el = None

        href = link_el.get("href") if link_el else None
        title = (title_el.get("alt") or "").strip() if source == "vidbox" and title_el else title_el.get_text(strip=True) if title_el else None
        # Fall back to the link's title/alt text if .item-title is absent.
        if not title and link_el is not None:
            title = (link_el.get("title") or "").strip() or None
        thumb_src = image_el.get("src") if image_el is not None else _extract_image_src(card)

        if not (href and title):
            # Skip malformed cards rather than crashing the whole scrape.
            logger.debug("Skipping card with missing href/title")
            continue

        if source == "current" and _is_tv_show_href(href):
            continue
        if source in ("bingeflix", "vidbox") and f"/{content_type}/" not in href:
            continue

        slug = _slug_from_href(href, base_url)
        if not slug or slug in seen:
            continue
        seen.add(slug)

        movies.append(
            Movie(
                title=title,
                # Normalize relative URLs so the frontend can load them directly.
                thumbnail_url=urljoin(base_url, thumb_src) if thumb_src else "",
                detail_page_slug=slug,
                source=source,
                content_type=content_type,
            )
        )

    return movies


async def scrape_catalog(source: str = "current", content_type: str = "movie", query: str = "", page: int = 1) -> list[Movie]:
    """Render one source/type catalog page and return structured media data."""
    if source == "bingeflix":
        base_url = settings.BINGEFLIX_BASE_URL
        if query:
            catalog_url = urljoin(base_url, f"/search?tab=movies-tv&q={quote_plus(query)}&page={page}")
        else:
            path = "/movies" if content_type == "movie" else "/tv"
            catalog_url = urljoin(base_url, f"{path}?page={page}")
        selector = "article.poster-card"
    elif source == "vidbox":
        base_url = settings.VIDBOX_BASE_URL
        path = "/search"
        params = f"type={content_type}"
        if query:
            params += f"&q={quote_plus(query)}"
        catalog_url = urljoin(base_url, f"{path}?{params}&page={page}")
        selector = f'a[href^="/{content_type}/"]'
    else:
        base_url = settings.TARGET_BASE_URL
        catalog_url = urljoin(base_url, settings.CATALOG_PATH)
        selector = settings.CATALOG_ITEM_SELECTOR
    html = await fetch_rendered_html(catalog_url, selector)
    if source == "current" and any(
        marker in html.lower() for marker in ("just a moment...", "security verification", "cloudflare")
    ):
        raise RuntimeError("Current source is blocked by its Cloudflare security challenge")
    movies = parse_catalog(html, base_url, source, content_type)
    if source == "current" and not movies:
        raise RuntimeError(
            "Current source returned no catalog items; its page structure or bot protection may have changed"
        )
    return movies
