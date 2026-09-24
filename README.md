---
title: Movie Directory
emoji: 🎬
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# Local Movie Directory

A localhost-only FastAPI app that scrapes a movie catalog from a JS-rendered
site (via Playwright + BeautifulSoup), resolves per-title embed/stream URLs
via network interception, and proxies them locally so they can be played in
an `<iframe>` or `<video>` - including HLS (`.m3u8`) streams, whose manifest
and segments are rewritten and re-proxied so the whole playlist plays through
this app rather than just the first request.

## ⚠️ Before you use this

This scaffold ships pointed at a **placeholder** target and will not work
until you configure it against a real site (see Setup below - it's done via
`.env`, not by editing code). Only point it at a site you own, or one whose
terms of service / robots.txt explicitly permit scraping and re-embedding its
video content. Scraping copyrighted video streams from a site you don't have
rights to and re-serving them (even locally) may violate that site's ToS and
copyright law in your jurisdiction. You are responsible for how you use this.

## Architecture

```
movie-directory/
├── app/
│   ├── main.py             FastAPI app + routes
│   ├── config.py           All target-site config (URLs, selectors, headers)
│   ├── models.py           Pydantic response models
│   ├── scraper_catalog.py  Playwright render + BeautifulSoup parse -> Movie list
│   ├── scraper_embed.py    Playwright network interception -> embed URL
│   └── proxy.py            httpx fetch w/ spoofed headers + header stripping
├── frontend/                React + Vite app (see frontend/README.md for its own commands)
│   ├── src/
│   │   ├── App.jsx          Layout: sidebar, hero, browse grid, player modal
│   │   ├── api.js           Thin fetch wrapper around the backend endpoints
│   │   ├── hooks/           useCatalog (fetch catalog), usePlayer (hls.js wiring)
│   │   └── components/      Sidebar, TopBar, Hero, MovieGrid, MovieCard, PlayerModal
│   ├── dist/                Built output FastAPI serves (git-ignored, run `npm run build`)
│   └── package.json
├── requirements.txt
└── README.md
```

Flow:

1. **Catalog**: `GET /api/catalog` launches headless Chromium, navigates to
   `TARGET_BASE_URL + CATALOG_PATH`, waits for `CATALOG_ITEM_SELECTOR`
   (`.list-movie .col`), grabs `page.content()`, and parses it with
   BeautifulSoup into `Movie` objects using `LINK_SELECTOR` (`a.poster`),
   `TITLE_SELECTOR` (`.item-title`), and `THUMBNAIL_SELECTOR` (`picture img`).
   Results are cached in-memory for `CATALOG_CACHE_TTL` seconds so the
   frontend doesn't trigger a fresh browser launch on every page load.

2. **Embed resolution**: `GET /api/get_embed/{slug}` launches another
   headless browser, arms `page.on("request", ...)` and frame-navigation
   listeners *before* navigating, visits `DETAIL_PATH_TEMPLATE` for the slug,
   clicks `PLAY_BUTTON_SELECTOR` (`a.d-block.cover`), and resolves as soon as
   a request or iframe URL matches one of `EMBED_URL_PATTERNS`.

3. **Proxy**: `GET /api/proxy_embed?url=...` re-fetches that embed URL
   server-side with a spoofed `Referer`/`User-Agent`/`Origin` (many embed/CDN
   hosts check these to block hotlinking). The URL is only fetched if
   `is_allowed_embed_url()` (`app/proxy.py`) passes - host must be the target
   site or in `ALLOWED_EMBED_HOSTS`, must not resolve to a loopback/private
   address, and must match `PROXY_ALLOWED_URL_PATTERNS`. Three response
   paths, based on the URL:
   - **`.m3u8` (HLS playlist)**: fetched and text-rewritten - every segment,
     variant-playlist, and key URI it references is resolved to an absolute
     URL and rewritten to route back through this same endpoint, so the
     player's *entire* playlist (not just the manifest) plays through the
     proxy. See `fetch_hls_manifest()`.
   - **Other direct media (`.mp4`/`.ts`/...)**: streamed with `Range`
     request support via `stream_media()`, so `<video>` seeking/scrubbing
     works without buffering the whole file server-side first.
   - **HTML embed pages**: buffered, `X-Frame-Options`/CSP stripped, and
     root-relative asset URLs rewritten to absolute, for use in an
     `<iframe>`.

4. **Image proxy**: `GET /api/proxy_image?url=...` does the same
   spoofed-header re-fetch for poster thumbnails, gated by a separate
   `ALLOWED_IMAGE_HOSTS` allowlist (`is_allowed_image_url()`). Useful when an
   image CDN rate-limits or blocks anonymous hotlink bursts with no
   `Referer` (which the browser won't send cross-origin from `localhost`).

5. **Frontend**: a React + Vite app (`frontend/`) - sidebar, a hero banner for
   the first catalog title, a searchable browse grid, and a player modal.
   Clicking (or hovering, via `/api/prefetch_embed`) a card calls
   `/api/get_embed/{slug}` through `usePlayer()` and renders the result based
   on the response's `is_hls`/`is_media` flags: `<video>` +
   [hls.js](https://github.com/video-dev/hls.js) for HLS (native `<video
   src>` in Safari, which supports HLS directly), plain `<video src>` for
   other direct media, or an `<iframe>` for HTML embed pages. There's no
   backend for search, watchlists, or social features - search filters the
   already-fetched catalog client-side, and nothing else from a typical
   streaming-site mockup (parties, "friends watching", coming soon) is wired
   up, since there's no real data behind it.

## Setup

### 1. Create a virtual environment and install dependencies

```powershell
cd "D:\Personal Web Apps\movie-directory"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Install Playwright's browser binaries

Playwright needs its own bundled browsers (separate from any browser already
on your machine) - the pip package alone is not enough:

```powershell
playwright install chromium
```

(Use `playwright install` with no argument to grab all browsers, but
Chromium alone is sufficient for this project.)

If you hit missing OS dependency errors on Linux, run
`playwright install-deps` as well (not usually needed on Windows).

### 3. Configure the target site via `.env`

Don't put the real URL in `app/config.py` - that file stays generic and
`.env` (gitignored) holds the actual target so you never accidentally commit
it. Copy the template and fill it in:

```powershell
copy .env.example .env
notepad .env
```

At minimum, set:

```
MOVIEDIR_TARGET_BASE_URL=https://the-real-site.example
```

Everything else in `.env.example` is optional and only needed if the real
site's structure differs from the defaults already baked into
`app/config.py` (`CATALOG_ITEM_SELECTOR=.list-movie .col`,
`LINK_SELECTOR=a.poster`, `TITLE_SELECTOR=.item-title`,
`THUMBNAIL_SELECTOR=picture img`, `DETAIL_PATH_TEMPLATE=/movie/{slug}/`,
`PLAY_BUTTON_SELECTOR=a.d-block.cover`). Any setting in `config.py` can be
overridden the same way, prefixed with `MOVIEDIR_` - `pydantic-settings`
loads `.env` automatically.

Once you know the embed/CDN host (check the `embed_url` returned by
`/api/get_embed/{slug}`), add it too, or the proxy will reject it:

```
MOVIEDIR_ALLOWED_EMBED_HOSTS=["cdn.example.com"]
```

If different titles route through different CDN backends (common with
multi-server players), add every host you encounter - the allowlist is
opt-in per host, not a wildcard on the domain. Poster thumbnails go through
a separate `MOVIEDIR_ALLOWED_IMAGE_HOSTS` list.

### 4. Build the frontend

FastAPI serves the frontend's built output (`frontend/dist/`), not its
source, so it needs a one-time (and after any frontend edit) build step:

```powershell
cd frontend
npm install
npm run build
cd ..
```

### 5. Run the app

```powershell
python run.py
```

**On Windows, use `run.py`, not `uvicorn --reload` directly.** uvicorn's
`--reload` mode force-sets `WindowsSelectorEventLoopPolicy` on Windows, which
cannot spawn subprocesses - and Playwright launches Chromium as one, so
`/api/catalog` and `/api/get_embed` fail with `NotImplementedError` under
plain `uvicorn --reload`. `run.py` sets `WindowsProactorEventLoopPolicy` and
runs without `--reload` to avoid that. (On macOS/Linux, plain
`uvicorn app.main:app --reload --port 8000` works fine.)

`run.py` doesn't auto-reload on file changes - restart it (Ctrl+C, then
`python run.py` again) after editing backend code, and re-run `npm run
build` after editing frontend code (`python run.py` serves whatever is
currently in `frontend/dist/`, not live source).

Then open **http://localhost:8000** in your browser.

**Frontend development** (hot-reload instead of rebuild-per-change): run
`python run.py` in one terminal and `npm run dev` (inside `frontend/`) in
another, then open the Vite dev server's URL (usually
**http://localhost:5173**) instead of :8000. Vite proxies `/api` calls to
the FastAPI backend on :8000 (see `frontend/vite.config.js`) while serving
the frontend itself with hot module reload.

## Notes & caveats

- **Performance**: `app/browser_pool.py` keeps one warm Chromium instance
  alive across requests (a fresh `BrowserContext` per capture, not a fresh
  browser process) - see its docstring. `/api/get_embed/{slug}` also races
  the play-button wait against the stream capture itself, blocks known ad/
  tracking hosts during capture (`BLOCKED_AD_HOSTS`/`BLOCKED_RESOURCE_TYPES`
  in `config.py`), and the frontend prefetches on hover
  (`/api/prefetch_embed`) so a click shortly after often returns instantly
  from cache (`EMBED_CACHE_TTL` in `app/main.py`).
- **Selector fragility**: scrapers built against a specific site's DOM will
  break when that site changes its markup. Centralizing selectors in
  `config.py` keeps fixes to one file.
- **The proxy is intentionally narrow**: `is_allowed_embed_url()` in
  `app/proxy.py` requires the host to be the target site or explicitly listed
  in `ALLOWED_EMBED_HOSTS`, and rejects hosts resolving to loopback/private/
  link-local addresses - that check is never skipped for any host. Hosts in
  `ALLOWED_EMBED_HOSTS` additionally skip the `PROXY_ALLOWED_URL_PATTERNS`
  substring check (some CDNs disguise segment URLs with a misleading
  extension like `.html`); the target site's own default host still requires
  a pattern match. Don't widen this to a generic `?url=` passthrough, and
  don't add a host to `ALLOWED_EMBED_HOSTS` unless you've actually verified
  it's the movie site's real CDN - either mistake turns the endpoint into an
  open proxy/SSRF vector anyone on your network could abuse.
- **Headless visibility**: set `HEADLESS: bool = False` in `config.py`
  temporarily if you need to visually debug why a selector isn't matching
  or a click isn't triggering the expected network request.
- **Playwright's bundled Chromium has no H.264/AAC decoder** - if you're
  debugging playback issues by scripting Playwright against this app (rather
  than using a real browser), a stream can appear to hang forever at 0%
  buffered even though every request succeeds and the manifest parses fine;
  check `MediaSource.isTypeSupported('audio/mp4;codecs=mp4a.40.2')` in that
  context before assuming it's an app bug. Real Chrome/Edge/Firefox all
  support these codecs natively and are unaffected.
