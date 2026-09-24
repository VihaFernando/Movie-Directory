"""
Local proxy for iframe/video playback.

Fetches the embed URL server-side with a spoofed Referer/User-Agent (embed
hosts commonly hotlink-protect on those) and drops the framing-restriction
headers so the response renders inside our own <iframe> (HTML embed pages)
or <video> (direct media files) on localhost.

Direct media (.mp4/.m3u8/...) is proxied via a streaming, Range-aware path
(see fetch_media_stream) rather than buffered in memory - browsers issue
Range requests for seeking, and a movie-length file would otherwise be
fully downloaded server-side before a single byte reaches the client.

Security note: this endpoint fetches and re-serves a URL supplied by the
caller. Without validation that is a textbook SSRF / open-relay hole, so
is_allowed_embed_url() gates it on an explicit host allowlist. Do not relax
it to a pattern-only check - "https://attacker.test/embed/" matches a naive
substring test while pointing anywhere.
"""
import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings

logger = logging.getLogger(__name__)


class StreamAuthError(Exception):
    """The CDN rejected the stream URL's access token.

    These links are minted per playback session and expire (observed: a 4h
    JWT carrying an ip_cidr claim), so a URL captured earlier can go dead
    while the page still holds it. Distinguished from a generic upstream
    error because the fix is specific and automatic: discard the cached
    capture and resolve the title again, rather than retrying the same
    dead URL.
    """


# Extensions that are direct media, not an HTML embed page - these get
# streamed with Range support rather than buffered.
_MEDIA_EXTENSIONS = (".mp4", ".m3u8", ".ts", ".webm", ".mkv")


def _url_looks_like(url: str, extensions: tuple[str, ...]) -> bool:
    """True if `url` ends with one of `extensions`, checking both the parsed
    path and the raw URL string.

    Some wrapper/relay CDNs pass the real target as a query value rather
    than a real path (e.g. "https://relay.example/?t=/https://origin/x.m3u8"),
    so urlparse(url).path is just "/" and a path-only check misses it. A
    plain string suffix check on the full URL catches that case too, at the
    minor cost of also matching a literal ".m3u8" appearing coincidentally
    at the very end of an unrelated query value - an acceptable trade-off
    here since a false positive only routes a URL through the HLS/media
    proxy path instead of the generic one, not a security issue.
    """
    lowered = url.lower()
    return lowered.endswith(extensions) or urlparse(url).path.lower().endswith(extensions)


def is_direct_media_url(url: str) -> bool:
    """True if `url` looks like a direct media file rather than an HTML
    embed page."""
    return _url_looks_like(url, _MEDIA_EXTENSIONS)


def is_hls_manifest_url(url: str) -> bool:
    """True if `url` is an HLS playlist (.m3u8), which needs text rewriting
    rather than raw byte streaming - see fetch_hls_manifest."""
    return _url_looks_like(url, (".m3u8",))


# --- Poster/thumbnail image cache ---
#
# Why this exists: /api/proxy_image used to open a brand-new httpx client
# and hit wsrv.nl -> image.tmdb.org fresh on every single request, for every
# poster, on every page load, for every user - no caching at any layer
# beyond the browser's own (which only helps a repeat visit in the SAME
# browser). A homepage with ~20 posters meant ~20 uncached two-hop fetches
# every time it opened.
#
# Posters don't change once published, so the fix is a permanent on-disk
# cache keyed by URL hash: the first request for a given poster pays the
# network cost, and every request after that - from any user, across
# restarts - is a local file read. Disk rather than Mongo because this is
# binary blob storage, not queryable data; a plain file is simpler and
# faster for this than GridFS.
_IMAGE_CACHE_DIR = Path("image_cache")

# One shared client for all image fetches, mirroring app/tmdb.py's
# _get_client() pattern - opening a fresh httpx.AsyncClient per image threw
# away connection pooling and TLS session reuse, which matters when many
# posters load concurrently on one page.
_image_client: httpx.AsyncClient | None = None
_image_client_lock = asyncio.Lock()

# Per-URL locks so two concurrent requests for the SAME poster (e.g. it
# appears in both a homepage grid and a "trending" row, both loading at
# once) share one fetch instead of both missing the cache and hitting
# wsrv.nl/TMDB independently. Bounded the same way _discovered_urls is in
# this file - an entry is dropped once its fetch completes, so this can
# only ever hold as many locks as there are in-flight image fetches.
_image_fetch_locks: dict[str, asyncio.Lock] = {}


async def _get_image_client() -> httpx.AsyncClient:
    global _image_client
    if _image_client is not None and not _image_client.is_closed:
        return _image_client
    async with _image_client_lock:
        if _image_client is not None and not _image_client.is_closed:
            return _image_client
        _image_client = httpx.AsyncClient(follow_redirects=True, timeout=15.0)
        return _image_client


async def close_image_client() -> None:
    """Close the shared image-fetch client. Called from the app's shutdown
    hook, mirroring tmdb.close()/browser_pool.close_browser()."""
    global _image_client
    if _image_client is not None and not _image_client.is_closed:
        await _image_client.aclose()
    _image_client = None


def _image_cache_path(url: str) -> Path:
    """Cache file path for `url`, named by its hash plus a content-type-ish
    suffix isn't needed - the content-type is stored in a sibling .meta
    file since a cached poster's extension can't be inferred reliably from
    the URL (wsrv.nl wraps the real extension in a query param)."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _IMAGE_CACHE_DIR / digest[:2] / digest


async def _load_cached_image(url: str) -> tuple[str, bytes] | None:
    """Return (content_type, body) from disk if this URL was fetched
    before, else None. Runs the blocking file I/O in a thread so it never
    stalls the event loop other requests are running on."""
    path = _image_cache_path(url)

    def _read() -> tuple[str, bytes] | None:
        meta_path = path.with_suffix(".meta")
        if not path.exists() or not meta_path.exists():
            return None
        try:
            content_type = meta_path.read_text(encoding="utf-8").strip()
            return content_type, path.read_bytes()
        except OSError:
            return None

    return await asyncio.to_thread(_read)


async def _save_cached_image(url: str, content_type: str, body: bytes) -> None:
    """Persist a fetched image to disk so future requests skip the network
    entirely. Best-effort: a failed write degrades to "fetch again next
    time", never breaks the response already being served."""
    path = _image_cache_path(url)

    def _write() -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write via a temp file then replace, so a request reading the
            # cache mid-write can never see a truncated file.
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
            path.with_suffix(".meta").write_text(content_type, encoding="utf-8")
        except OSError:
            logger.warning("Could not write image cache entry for %s", url, exc_info=True)

    await asyncio.to_thread(_write)


# Response headers that would otherwise stop us framing the content locally.
_STRIPPED_RESPONSE_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    # Hop-by-hop / encoding headers that must not be forwarded verbatim.
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
}


# Hosts that our OWN headless capture observed serving the stream for a
# title, remembered so the frontend's follow-up /api/proxy_embed request for
# that same URL passes the guardrail.
#
# Why this exists: Vidbox hands each title a single-use manifest on a
# randomly-named host ("opalescentoblivion.space", "fulcrumoffolklore.space",
# ...) that differs per title and rotates over time, so it cannot be put in
# a static allowlist the way a fixed CDN can.
#
# Why it is not a hole: entries are only ever added by record_captured_url()
# from capture_embed_url()'s own Playwright observation - never from
# anything a caller sends. A client cannot get a host in here by asking for
# it; it can only ask for a URL we already independently discovered while
# loading the configured target site. The scheme and private-address checks
# in _is_allowed_url() still apply to these hosts, so this widens *which
# public host* may be fetched, not the SSRF boundary itself.
_discovered_urls: OrderedDict[str, None] = OrderedDict()

# Bounded so a long-running server cannot accumulate hosts without limit;
# oldest entries fall out first.
_DISCOVERED_URL_LIMIT = 512


def record_captured_url(url: str) -> None:
    """Remember a stream URL that our own capture observed, so the player's
    subsequent proxy request for it is allowed. See _discovered_urls."""
    if not url:
        return
    _discovered_urls[url] = None
    _discovered_urls.move_to_end(url)
    while len(_discovered_urls) > _DISCOVERED_URL_LIMIT:
        _discovered_urls.popitem(last=False)


def _is_discovered_host(host: str) -> bool:
    """True if `host` served a stream URL our own capture discovered."""
    return any(
        (urlparse(u).hostname or "").lower() == host for u in _discovered_urls
    )


# Hosts observed to reject a spoofed Referer/Origin (see the inverted-hotlink
# comment in stream_media()), keyed by hostname so the next request to the
# same host tries the header-less variant first instead of paying a
# guaranteed 403 + retry round trip. This is a hint only: whichever variant
# is tried first, a 403 always falls back to the other one, so a wrong or
# stale guess costs one extra request rather than breaking playback - see
# fetch_hls_manifest/stream_media. Bounded the same way _discovered_urls is,
# since these hosts are mostly one-shot, randomly-named mirrors.
_no_spoof_hosts: OrderedDict[str, None] = OrderedDict()
_NO_SPOOF_HOST_LIMIT = 512


def _remember_no_spoof_host(url: str) -> None:
    host = urlparse(url).hostname
    if not host:
        return
    host = host.lower()
    _no_spoof_hosts[host] = None
    _no_spoof_hosts.move_to_end(host)
    while len(_no_spoof_hosts) > _NO_SPOOF_HOST_LIMIT:
        _no_spoof_hosts.popitem(last=False)


def _prefers_no_spoof(url: str) -> bool:
    host = urlparse(url).hostname
    return bool(host) and host.lower() in _no_spoof_hosts


def _allowed_hosts(extra_hosts: tuple[str, ...]) -> set[str]:
    """Hosts the proxy may fetch from: the target site plus the given
    explicitly configured CDN/embed/image hosts."""
    hosts = {h.lower() for h in extra_hosts if h}
    hosts.update(h.lower() for h in settings.BINGEFLIX_EMBED_HOSTS if h)
    hosts.update(h.lower() for h in settings.VIDBOX_EMBED_HOSTS if h)
    for base_url in (settings.TARGET_BASE_URL, settings.BINGEFLIX_BASE_URL):
        target_host = urlparse(base_url).hostname
        if target_host:
            hosts.add(target_host.lower())
    return hosts


def _host_matches(host: str, allowed: str) -> bool:
    """Exact host match, or a subdomain of an allowed host."""
    return host == allowed or host.endswith("." + allowed)


def _resolves_to_private_address(host: str) -> bool:
    """True if `host` resolves to a loopback/private/link-local address.

    Blocks the DNS-rebinding style bypass where an allowlisted name points at
    internal infrastructure. Fails closed on resolution errors.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def _is_allowed_url(url: str, allowed_hosts: tuple[str, ...], url_patterns: tuple[str, ...]) -> bool:
    """Shared guardrail core: scheme, host allowlist, private-IP check, and
    an optional substring pattern match (skipped if `url_patterns` is empty).
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        logger.warning("Rejected proxy URL with scheme %r", parsed.scheme)
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False

    allowed = _allowed_hosts(allowed_hosts)
    if not any(_host_matches(host, entry) for entry in allowed):
        logger.warning("Rejected proxy URL: host %r not in allowlist %r", host, sorted(allowed))
        return False

    if _resolves_to_private_address(host):
        logger.warning("Rejected proxy URL: host %r resolves to a private/internal address", host)
        return False

    if url_patterns and not any(pattern in url for pattern in url_patterns):
        logger.warning("Rejected proxy URL: no pattern matched for %s", url)
        return False

    return True


def is_allowed_embed_url(url: str) -> bool:
    """Guardrail for /api/proxy_embed.

    A URL is proxied only if all of the following hold:
      * scheme is http or https (no file://, gopher://, etc.)
      * host is the target site or an explicitly allowlisted embed host
        (or a subdomain of one)
      * host does not resolve to a loopback/private/link-local address
      * ADDITIONALLY, if the host is the target site itself (not one of the
        explicitly configured ALLOWED_EMBED_HOSTS): the URL must match one
        of the known embed/stream patterns.

    The pattern-match requirement is deliberately skipped for hosts in
    ALLOWED_EMBED_HOSTS: that list is opt-in and operator-configured
    specifically as "this is the movie site's video CDN", so once a host is
    on it, any path on that host is trusted content, regardless of what
    extension its URLs happen to use - some CDNs disguise segment URLs with
    a misleading extension (e.g. ".html") specifically to dodge naive
    pattern checks. The pattern check still applies to the target site's own
    host, which is a broad default rather than something explicitly vetted
    for this purpose. The host allowlist and private-IP check above are the
    actual SSRF/open-relay boundary and are never skipped for any host.

    Note that ALLOWED_EMBED_HOSTS starts empty: until you add the real embed
    host, only the target site's own host is proxied. That is deliberate -
    the allowlist is opt-in, never a wildcard.
    """
    host = (urlparse(url).hostname or "").lower()

    # A host our own capture discovered for this stream (see
    # _discovered_urls) is trusted the same way an explicitly configured
    # embed host is - it got here by our observation, not by caller input -
    # but it still goes through the scheme/private-address checks below.
    if _is_discovered_host(host):
        return _is_allowed_url(url, settings.ALLOWED_EMBED_HOSTS + (host,), ())

    explicit_hosts = settings.ALLOWED_EMBED_HOSTS + settings.BINGEFLIX_EMBED_HOSTS + settings.VIDBOX_EMBED_HOSTS
    is_explicit_embed_host = any(_host_matches(host, entry.lower()) for entry in explicit_hosts if entry)
    patterns = () if is_explicit_embed_host else settings.PROXY_ALLOWED_URL_PATTERNS
    return _is_allowed_url(url, settings.ALLOWED_EMBED_HOSTS, patterns)


def is_allowed_image_url(url: str) -> bool:
    """Guardrail for /api/proxy_image - same host-allowlist/SSRF protections
    as is_allowed_embed_url, but against ALLOWED_IMAGE_HOSTS and without an
    embed-pattern requirement (poster URLs don't follow EMBED_URL_PATTERNS)."""
    return _is_allowed_url(url, settings.ALLOWED_IMAGE_HOSTS, ())


async def fetch_sanitized_embed(url: str) -> tuple[str, bytes]:
    """Fetch `url` with spoofed headers; return (content_type, body bytes)."""
    headers = {
        "User-Agent": settings.SPOOFED_USER_AGENT,
        "Referer": settings.TARGET_BASE_URL,
        # Some hosts check Origin in addition to Referer.
        "Origin": settings.TARGET_BASE_URL,
        "Accept": "*/*",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()

        # A redirect can land somewhere outside the allowlist, so re-validate
        # the URL we actually ended up fetching.
        final_url = str(resp.url)
        if final_url != url and not is_allowed_embed_url(final_url):
            raise ValueError(f"Redirected to a disallowed URL: {final_url}")

    content_type = resp.headers.get("content-type", "text/html")

    # Only rewrite markup; binary media (video segments, images) passes through.
    if "html" not in content_type.lower():
        return content_type, resp.content

    parsed = urlparse(final_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    body = resp.text
    is_bingeflix_player = (parsed.hostname or "").lower() in {
        host.lower() for host in settings.BINGEFLIX_EMBED_HOSTS
    }

    if not is_bingeflix_player:
        # Preserve the original source behavior: its HTML player owns its
        # scripts/assets and needs root-relative URLs resolved upstream.
        body = body.replace('src="/', f'src="{base}/').replace("src='/", f"src='{base}/")
        body = body.replace('href="/', f'href="{base}/').replace("href='/", f"href='{base}/")
        return content_type, body.encode("utf-8")

    # Keep only Next.js assets same-origin. Its runtime constructs later
    # chunk URLs from /_next/, while other root paths belong to the player
    # host and should continue resolving there.
    body = body.replace('src="/_next/', 'src="__LOCAL_NEXT__/')
    body = body.replace("src='/_next/", "src='__LOCAL_NEXT__/")
    body = body.replace('href="/_next/', 'href="__LOCAL_NEXT__/')
    body = body.replace("href='/_next/", "href='__LOCAL_NEXT__/")
    body = body.replace('src="/', f'src="{base}/').replace("src='/", f"src='{base}/")
    body = body.replace('href="/', f'href="{base}/').replace("href='/", f"href='{base}/")
    body = body.replace('src="__LOCAL_NEXT__/', 'src="/_next/')
    body = body.replace("src='__LOCAL_NEXT__/", "src='/_next/")
    body = body.replace('href="__LOCAL_NEXT__/', 'href="/_next/')
    body = body.replace("href='__LOCAL_NEXT__/", "href='/_next/")

    # Remove analytics/ad script tags from the upstream document. Required
    # player bundles are root-relative /_next assets or explicitly hosted on
    # the configured player hosts; everything else is nonessential here.
    soup = BeautifulSoup(body, "html.parser")
    player_hosts = {host.lower() for host in settings.BINGEFLIX_EMBED_HOSTS}
    for script in soup.find_all("script", src=True):
        script_url = urlparse(script["src"])
        script_host = (script_url.hostname or "").lower()
        if script_url.path.startswith("/_next/"):
            continue
        if script_host not in player_hosts:
            script.decompose()
    body = str(soup)
    # Next.js also serializes the analytics component into hydration data;
    # neutralize that payload so hydration cannot recreate the removed tag.
    body = body.replace("https://www.googletagmanager.com/gtag/js?id=G-JMWN78FZNK", "about:blank")
    body = body.replace(
        "window.dataLayer = window.dataLayer || [];function gtag(){dataLayer.push(arguments);}gtag('js', new Date());gtag('config', 'G-JMWN78FZNK');",
        "void 0",
    )

    return content_type, body.encode("utf-8")


async def fetch_player_asset(path: str, query: str = "") -> tuple[str, bytes]:
    """Fetch a configured player's same-origin Next.js asset."""
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("Invalid player asset path")

    headers = {
        "User-Agent": settings.SPOOFED_USER_AGENT,
        "Referer": settings.BINGEFLIX_BASE_URL,
        "Origin": settings.BINGEFLIX_BASE_URL,
        "Accept": "*/*",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        for host in settings.BINGEFLIX_EMBED_HOSTS:
            asset_url = f"https://{host}/_next/{path}"
            if query:
                asset_url += f"?{query}"
            if not is_allowed_embed_url(asset_url):
                continue
            response = await client.get(asset_url, headers=headers)
            if response.status_code == 200:
                content_type = response.headers.get("content-type", "application/octet-stream")
                body = response.content
                if path.endswith(".js") and not path.endswith("webpack-c6e1ad769d74aabf.js"):
                    # Application chunks contain unrelated popunder,
                    # PDF-plugin, and dynamic ad-script behaviors. Keep the
                    # player code, but prevent those chunks from creating
                    # executable third-party script elements. The webpack
                    # runtime is excluded because it must load real Next.js
                    # chunks dynamically.
                    text = body.decode("utf-8", "replace")
                    text = text.replace("window.open", "(()=>null)")
                    text = text.replace("data:application/pdf;base64,aG1t", "about:blank")
                    text = re.sub(
                        r"\.createElement\(\s*([\"'])script\1\s*\)",
                        '.createElement("div")',
                        text,
                    )
                    body = text.encode("utf-8")
                return content_type, body

    raise ValueError("Player asset was not found on configured hosts")


def _proxy_media_url(absolute_url: str, kind: str) -> str:
    """Build a local /api/proxy_embed URL that resolves back to
    `absolute_url` - used to rewrite manifest entries (segments, variant
    playlists, keys) so the player fetches them through us instead of
    directly (avoiding hotlink-protection failures and keeping every
    request inside the same guardrail as the original .m3u8).

    `kind` is "manifest" for a nested .m3u8 (variant playlist) or "segment"
    for anything else referenced from a manifest (media segments, fMP4 init
    data, encryption keys). It's carried as a &hint= query param so the
    proxy_embed route knows how to handle the URL without re-sniffing its
    extension - some CDNs disguise segment URLs with a misleading extension
    (e.g. ".html") specifically to dodge naive "is this media" checks, and
    since this URL only exists because we already found it inside a
    validated manifest, we already know for certain what it is.
    """
    return f"/api/proxy_embed?hint={kind}&url={quote(absolute_url, safe='')}"


async def fetch_hls_manifest(url: str) -> tuple[str, bytes]:
    """Fetch an .m3u8 playlist and rewrite every URI line it contains to
    point back through /api/proxy_embed, resolved to an absolute URL first.

    Handles both playlist types:
      * media playlists - #EXTINF lines followed by a segment URI, plus an
        optional #EXT-X-MAP line carrying a URI="..." attribute (fMP4
        initialization segment - required for playback, so it must be
        proxied like any other segment, not left pointing at the origin)
      * master playlists - #EXT-X-STREAM-INF lines followed by a variant
        .m3u8 URI, plus #EXT-X-KEY / #EXT-X-MEDIA lines carrying a
        URI="..." attribute (encryption keys, alternate audio/subtitle
        tracks)

    Segment/variant URIs in these playlists are commonly relative (e.g.
    "00000.ts"), so each is resolved against `url` (the manifest's own
    location) before being wrapped - relative resolution has to happen here,
    server-side, because the proxied URL served to the browser no longer
    shares a base path with the original CDN.
    """
    headers = {
        "User-Agent": settings.SPOOFED_USER_AGENT,
        "Referer": settings.TARGET_BASE_URL,
        "Origin": settings.TARGET_BASE_URL,
        "Accept": "*/*",
    }
    stripped_headers = {k: v for k, v in headers.items() if k not in ("Referer", "Origin")}
    no_spoof_first = _prefers_no_spoof(url)
    first_headers = stripped_headers if no_spoof_first else headers

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        resp = await client.get(url, headers=first_headers)
        # Same inverted hotlink rule as in stream_media(): a few CDNs 403 any
        # request carrying a Referer/Origin, so retry once with the other
        # header variant.
        if resp.status_code == 403:
            retry_headers = headers if no_spoof_first else stripped_headers
            logger.info(
                "403 with %s Referer/Origin; retrying manifest %s %s them",
                "no" if no_spoof_first else "spoofed",
                url,
                "with" if no_spoof_first else "without",
            )
            resp = await client.get(url, headers=retry_headers)
            if resp.status_code != 403 and not no_spoof_first:
                _remember_no_spoof_host(url)
        # The stream CDN answers 401 "no token" / 403 "invalid token" for a
        # link whose token is missing or expired. Surface that as its own
        # error so the caller can re-resolve instead of reporting an opaque
        # 502 for what is really just a stale link.
        if resp.status_code in (401, 403):
            raise StreamAuthError(
                f"stream link rejected by CDN ({resp.status_code}: "
                f"{resp.text.strip()[:64] or 'no detail'})"
            )
        resp.raise_for_status()
        final_url = str(resp.url)
        if final_url != url and not is_allowed_embed_url(final_url):
            raise ValueError(f"Redirected to a disallowed URL: {final_url}")
        text = resp.text

    def rewrite_uri_attr(line: str, kind: str) -> str:
        # Rewrites URI="..." on a tag line (#EXT-X-KEY / #EXT-X-MEDIA / #EXT-X-MAP).
        marker = 'URI="'
        start = line.find(marker)
        if start == -1:
            return line
        start += len(marker)
        end = line.find('"', start)
        if end == -1:
            return line
        original = line[start:end]
        absolute = urljoin(final_url, original)
        return line[:start] + _proxy_media_url(absolute, kind) + line[end:]

    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
        elif stripped.startswith("#EXT-X-MEDIA"):
            # URI here points at another manifest (an alternate audio/
            # subtitle track's own .m3u8), not raw segment bytes.
            out_lines.append(rewrite_uri_attr(line, "manifest"))
        elif stripped.startswith(("#EXT-X-KEY", "#EXT-X-MAP")):
            # URI here points at binary data (encryption key / fMP4 init
            # segment), not another manifest.
            out_lines.append(rewrite_uri_attr(line, "segment"))
        elif stripped.startswith("#"):
            # Any other tag line (#EXTINF, #EXT-X-STREAM-INF, etc.) - no URI
            # of its own, passed through unchanged.
            out_lines.append(line)
        else:
            # A bare URI line: either a media segment (following #EXTINF) or
            # a variant playlist (following #EXT-X-STREAM-INF). Both are
            # plain strings at this point with no tag to distinguish them,
            # so fall back to extension sniffing here specifically - CDNs
            # observed so far only obscure segment extensions, never a
            # variant playlist's, so this heuristic is safe in practice.
            absolute = urljoin(final_url, stripped)
            kind = "manifest" if is_hls_manifest_url(absolute) else "segment"
            out_lines.append(_proxy_media_url(absolute, kind))

    rewritten = "\n".join(out_lines)
    if text.endswith("\n"):
        rewritten += "\n"

    return "application/vnd.apple.mpegurl", rewritten.encode("utf-8")


async def fetch_image(url: str) -> tuple[str, bytes]:
    """Return a poster/thumbnail's (content_type, bytes), from the on-disk
    cache when available and from the network otherwise.

    Some image CDNs (e.g. Jetpack Photon on i2.wp.com) rate-limit or reject
    anonymous hotlink bursts with no Referer - the browser doesn't send one
    cross-origin from localhost, so fetching server-side with the target
    site's Referer avoids that. Enforces an image/* content-type so this
    can't be used to fetch and re-serve arbitrary non-image content.

    Posters don't change once published, so a cache hit is served straight
    from disk with no network call at all - see the image-cache block above
    for why this exists and _load_cached_image/_save_cached_image for the
    on-disk format.

    Concurrent requests for the SAME url share one fetch rather than
    racing (see _image_fetch_locks) - without this, the same poster loading
    twice at once (e.g. in two different rows on the homepage) would both
    miss the cache and both hit the network.
    """
    cached = await _load_cached_image(url)
    if cached is not None:
        return cached

    lock = _image_fetch_locks.setdefault(url, asyncio.Lock())
    async with lock:
        try:
            # Re-check: whoever held the lock first may have already
            # populated the cache while we were waiting for it.
            cached = await _load_cached_image(url)
            if cached is not None:
                return cached

            headers = {
                "User-Agent": settings.SPOOFED_USER_AGENT,
                "Referer": settings.TARGET_BASE_URL,
                "Accept": "image/*",
            }

            client = await _get_image_client()
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()

            final_url = str(resp.url)
            if final_url != url and not is_allowed_image_url(final_url):
                raise ValueError(f"Redirected to a disallowed URL: {final_url}")

            content_type = resp.headers.get("content-type", "")
            if not content_type.lower().startswith("image/"):
                raise ValueError(f"Expected an image response, got content-type {content_type!r}")

            body = resp.content
            await _save_cached_image(url, content_type, body)
            return content_type, body
        finally:
            # Drop the lock once nobody else is waiting on it, so
            # _image_fetch_locks doesn't grow forever across distinct
            # posters over the process lifetime. This runs INSIDE the
            # `async with lock` block, so lock.locked() is always True here
            # (we are the holder) - what actually matters is whether
            # anyone else is queued behind us, which asyncio.Lock exposes
            # only as its private waiter list. Falling back to "just drop
            # it" if that internal detail ever changes: a dropped lock
            # with a waiter still attached only costs that one waiter an
            # extra cache-miss-and-refetch, never incorrect behaviour.
            waiters = getattr(lock, "_waiters", None)
            if not waiters:
                _image_fetch_locks.pop(url, None)


_PASSTHROUGH_REQUEST_HEADERS = ("range", "if-range")
_PASSTHROUGH_RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
)


async def stream_media(url: str, request_headers: dict[str, str]):
    """Open a streaming, Range-aware proxy connection to a direct media URL.

    Returns (status_code, response_headers, async_byte_iterator, aclose).
    The caller must call `aclose()` once done consuming the iterator (a
    FastAPI StreamingResponse's `background` isn't used here to keep this
    function testable/standalone - see the route for wiring).
    """
    headers = {
        "User-Agent": settings.SPOOFED_USER_AGENT,
        "Referer": settings.TARGET_BASE_URL,
        "Origin": settings.TARGET_BASE_URL,
    }
    for name in _PASSTHROUGH_REQUEST_HEADERS:
        value = request_headers.get(name)
        if value:
            headers[name.title()] = value
    stripped_headers = {k: v for k, v in headers.items() if k not in ("Referer", "Origin")}

    # Some CDNs invert the usual hotlink rule: rather than requiring a
    # Referer from their own site, they 403 any request that carries a
    # Referer/Origin at all, because their real player fetches segments as
    # same-origin (which sends neither). Spoofing is still the right default
    # - most hosts here require it - so this only changes which variant is
    # tried first for a host _no_spoof_hosts has already seen reject
    # spoofing; either way a 403 falls back to the other variant.
    no_spoof_first = _prefers_no_spoof(url)
    first_headers = stripped_headers if no_spoof_first else headers

    client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
    req = client.build_request("GET", url, headers=first_headers)
    resp = await client.send(req, stream=True)

    if resp.status_code == 403:
        await resp.aclose()
        retry_headers = headers if no_spoof_first else stripped_headers
        logger.info(
            "403 with %s Referer/Origin; retrying %s %s them",
            "no" if no_spoof_first else "spoofed",
            url,
            "with" if no_spoof_first else "without",
        )
        req = client.build_request("GET", url, headers=retry_headers)
        resp = await client.send(req, stream=True)
        if resp.status_code != 403 and not no_spoof_first:
            _remember_no_spoof_host(url)

    final_url = str(resp.url)
    if final_url != url and not is_allowed_embed_url(final_url):
        await resp.aclose()
        await client.aclose()
        raise ValueError(f"Redirected to a disallowed URL: {final_url}")

    response_headers = {
        name: resp.headers[name] for name in _PASSTHROUGH_RESPONSE_HEADERS if name in resp.headers
    }

    async def aclose() -> None:
        await resp.aclose()
        await client.aclose()

    return resp.status_code, response_headers, resp.aiter_bytes(), aclose


def build_proxy_response_headers(content_type: str) -> dict[str, str]:
    """Headers for our own response - deliberately without the upstream
    framing restrictions, so the content is embeddable on localhost."""
    return {
        "Content-Type": content_type,
        "Cache-Control": "no-store",
        # No X-Frame-Options / CSP frame-ancestors => framable locally.
    }


async def fetch_subtitle(url: str) -> bytes | None:
    """Fetch a subtitle (.vtt) file. Returns None if it 404s (title has no
    subtitles in that language - not an error, just "nothing to show").

    Unlike fetch_image/fetch_sanitized_embed, this isn't gated by a caller-
    supplied URL guardrail: `url` is always built server-side from
    settings.SUBTITLE_URL_TEMPLATE + a known movie slug (see
    main.get_subtitle), never taken from request input, so there's no SSRF
    surface to validate here.
    """
    headers = {"User-Agent": settings.SPOOFED_USER_AGENT}
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content
