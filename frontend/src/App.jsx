import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchGenres, fetchIndexStatus, prefetchEmbed } from './api'
import { Navbar } from './components/Navbar'
import { useDebounced } from './hooks/useDebounced'
import { useDetail } from './hooks/useDetail'
import { usePlayer } from './hooks/usePlayer'
import { decodeUrl, useUrlSync } from './hooks/useUrlSync'
import { AdminPage } from './pages/AdminPage'
import { BrowsePage } from './pages/BrowsePage'
import { DetailPage } from './pages/DetailPage'
import { FavoritesPage } from './pages/FavoritesPage'
import { HomePage } from './pages/HomePage'
import { SearchPage } from './pages/SearchPage'
import { WatchPage } from './pages/WatchPage'

// Only Vidbox is enabled: the other two sources are blocked upstream
// (BingeFlix serves an automation-blocking iframe player, Cinetaro sits
// behind a Cloudflare challenge the scraper cannot pass). Their scrapers
// and config all remain in place server-side, so re-enabling one is a
// backend change rather than a rewrite.
const SOURCE = 'vidbox'

// `nav`/`openTitle` are still the actual router - there is no route table
// or <Route> tree anywhere. What changed is that the URL's query string is
// now kept in sync with them (see useUrlSync), purely so a refresh or a
// pasted link can restore the same view instead of always landing on Home.
// See useUrlSync.js for why this uses "/?..." rather than real paths. The
// default nav shape (view: 'home', ...) lives in useUrlSync's NAV_DEFAULTS
// now, since decodeUrl() always has to produce it anyway - kept in one place
// rather than duplicated here.

// Read once, outside React state, so it can seed BOTH nav and openTitle's
// initializers below without decoding the URL twice on first render.
const initialUrlState = decodeUrl()

function App() {
  const [nav, setNav] = useState(() => initialUrlState.nav)
  const [openTitle, setOpenTitle] = useState(() => initialUrlState.openTitle)
  const [query, setQuery] = useState(() => initialUrlState.query)
  const [genres, setGenres] = useState([])
  const [indexStatus, setIndexStatus] = useState(null)
  // Consumed once, right after mount, to auto-resume playback if the URL
  // that loaded this page had ?play=1 (a refresh or a shared link while
  // watching) - see the effect below that fires player.open().
  const pendingPlayRef = useRef(
    initialUrlState.wantsPlay
      ? { season: initialUrlState.season, episode: initialUrlState.episode }
      : null,
  )

  // The typed value drives the input; the debounced one drives the request,
  // so typing stays responsive while the backend sees one search per pause.
  const debouncedQuery = useDebounced(query, 450)
  const player = usePlayer()
  const detail = useDetail(openTitle?.slug ?? null, SOURCE, openTitle?.content_type ?? 'movie')

  useUrlSync({ nav, openTitle, player, query: debouncedQuery })

  // Restore playback after a refresh/shared link. Waits for the detail
  // fetch so the real poster is available (matching what a normal Play
  // click passes) rather than opening with an empty one.
  useEffect(() => {
    if (!pendingPlayRef.current || !openTitle || detail.status === 'loading') return
    const { season, episode } = pendingPlayRef.current
    pendingPlayRef.current = null
    const poster = detail.detail?.poster_url || openTitle.poster_url
    player.open(openTitle.slug, poster, SOURCE, openTitle.content_type, false, season, episode)
    // player.open is stable across renders (see usePlayer's useCallback), so
    // it is safe to omit - including it would not change when this fires,
    // and openTitle/detail.status are the actual triggers for this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openTitle, detail.status])

  // Restore state on browser Back/Forward. useUrlSync only ever pushes/
  // replaces the URL to follow state changes; it never reads popstate, so
  // without this a Back press would change the address bar but leave the
  // page showing whatever was on screen.
  useEffect(() => {
    const onPopState = () => {
      const restored = decodeUrl()
      setOpenTitle(restored.openTitle)
      setNav(restored.nav)
      setQuery(restored.query)
      if (restored.wantsPlay && restored.openTitle) {
        pendingPlayRef.current = { season: restored.season, episode: restored.episode }
      } else {
        player.close()
      }
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
    // player.close is stable (usePlayer's useCallback) - see the note above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Poll the index only while it is still building, then stop - once it is
  // ready there is nothing further to report, and polling forever would be
  // pointless traffic.
  useEffect(() => {
    let cancelled = false
    let timer

    const poll = async () => {
      try {
        const status = await fetchIndexStatus()
        if (cancelled) return
        setIndexStatus(status)
        if (status.status === 'building') timer = setTimeout(poll, 4000)
      } catch {
        // A failed status check is not worth surfacing; the pages below
        // degrade to "nothing here yet" on their own.
      }
    }
    poll()

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [])

  // Genres come from the indexed catalog, so they change as it fills.
  useEffect(() => {
    let cancelled = false
    fetchGenres(nav.contentType, SOURCE)
      .then((data) => !cancelled && setGenres(data.genres || []))
      .catch(() => !cancelled && setGenres([]))
    return () => {
      cancelled = true
    }
  }, [nav.contentType, indexStatus?.total])

  // Typing a search moves to the search view; clearing it returns home.
  useEffect(() => {
    if (debouncedQuery.trim()) {
      setOpenTitle(null)
      setNav((n) => ({ ...n, view: 'search' }))
    } else if (nav.view === 'search') {
      setNav((n) => ({ ...n, view: 'home' }))
    }
  }, [debouncedQuery])

  const navigate = useCallback((target) => {
    player.close()
    setOpenTitle(null)
    setNav((current) => ({ ...current, ...target }))
    window.scrollTo({ top: 0 })
  }, [player])

  // Opening a title shows its page; nothing is resolved until Play, so
  // browsing a series costs no stream capture.
  const openDetail = useCallback((item) => {
    setOpenTitle({ slug: item.slug, content_type: item.content_type, poster_url: item.poster_url })
    window.scrollTo({ top: 0 })
    // Start resolving the stream while the user reads the page. By the time
    // they press Play it is usually already done, turning a ~12s wait into
    // an instant open. Safe by construction: the backend coalesces on the
    // in-flight capture, so a real Play never starts a second one and the
    // worst case is exactly today's timing.
    prefetchEmbed(
      item.slug,
      SOURCE,
      item.content_type,
      item.content_type === 'tv' ? 1 : null,
      item.content_type === 'tv' ? 1 : null,
    )
  }, [])

  // Continue Watching card click: jump straight into playback at the saved
  // season/episode/position, rather than the detail page a plain poster
  // click opens - the whole point of that row is resuming in one click, not
  // re-navigating through a page the viewer has already seen.
  const resumeTitle = useCallback((entry) => {
    setOpenTitle({ slug: entry.slug, content_type: entry.content_type, poster_url: entry.poster_url })
    window.scrollTo({ top: 0 })
    player.open(
      entry.slug,
      entry.poster_url,
      entry.source || SOURCE,
      entry.content_type,
      false,
      entry.season ?? null,
      entry.episode ?? null,
      entry.progress_seconds || 0,
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const closeDetail = useCallback(() => {
    player.close()
    setOpenTitle(null)
  }, [player])

  // Warm the stream on hover so a click often finds it already resolved -
  // but only after the cursor rests on a card for a bit. Without this delay
  // every card the mouse merely passes over while scrolling triggers a real
  // scraper capture, which is wasted load for a hover that was never intent
  // to click.
  const hoverTimerRef = useRef(null)
  const hoverPrefetch = useCallback((item) => {
    clearTimeout(hoverTimerRef.current)
    hoverTimerRef.current = setTimeout(() => {
      prefetchEmbed(item.slug, SOURCE, item.content_type)
    }, 350)
  }, [])
  const hoverPrefetchCancel = useCallback(() => {
    clearTimeout(hoverTimerRef.current)
  }, [])

  const playTitle = useCallback(() => {
    if (!openTitle) return
    const poster = detail.detail?.poster_url || openTitle.poster_url
    player.open(openTitle.slug, poster, SOURCE, openTitle.content_type)
  }, [openTitle, detail.detail, player])

  const playEpisode = useCallback(
    (season, episode) => {
      if (!openTitle) return
      const poster = detail.detail?.poster_url || openTitle.poster_url
      player.open(openTitle.slug, poster, SOURCE, openTitle.content_type, false, season, episode)
    },
    [openTitle, detail.detail, player],
  )

  const currentEpisode =
    player.status !== 'idle' && player.season != null
      ? { season: player.season, episode: player.episode }
      : null

  // Clicking a suggested title while watching something else: stop the
  // current stream and open the new title's own detail page, exactly like
  // clicking a poster anywhere else in the app.
  const openSuggestion = useCallback((item) => {
    player.close()
    openDetail(item)
  }, [player, openDetail])

  const isWatching = player.status !== 'idle'

  return (
    <div className="min-h-screen bg-background">
      <Navbar
        view={nav.view}
        contentType={nav.contentType}
        genres={genres}
        query={query}
        onQueryChange={setQuery}
        onNavigate={navigate}
      />

      <main>
        {isWatching && openTitle ? (
          <WatchPage
            player={player}
            detail={detail}
            contentType={openTitle.content_type}
            onBack={player.close}
            episodes={detail.episodes}
            seasons={detail.seasons}
            selectedSeason={detail.selectedSeason}
            selectSeason={detail.selectSeason}
            onPlayEpisode={playEpisode}
            onOpenSuggestion={openSuggestion}
          />
        ) : openTitle ? (
          <DetailPage
            detail={detail}
            contentType={openTitle.content_type}
            onBack={closeDetail}
            onPlay={playTitle}
            onPlayEpisode={playEpisode}
            currentEpisode={currentEpisode}
            onOpenSimilar={openSuggestion}
          />
        ) : nav.view === 'admin' ? (
          <AdminPage />
        ) : nav.view === 'favorites' ? (
          <FavoritesPage onOpen={openDetail} onHover={hoverPrefetch} onHoverEnd={hoverPrefetchCancel} />
        ) : nav.view === 'search' ? (
          <SearchPage
            query={debouncedQuery}
            contentType={nav.contentType}
            onNavigate={navigate}
            onOpen={openDetail}
            onHover={hoverPrefetch}
            onHoverEnd={hoverPrefetchCancel}
          />
        ) : nav.view === 'browse' ? (
          <BrowsePage
            contentType={nav.contentType}
            genre={nav.genre}
            sort={nav.sort}
            genres={genres}
            onNavigate={navigate}
            onOpen={openDetail}
            onHover={hoverPrefetch}
            onHoverEnd={hoverPrefetchCancel}
            indexStatus={indexStatus}
          />
        ) : (
          <HomePage
            onOpen={openDetail}
            onResume={resumeTitle}
            onHover={hoverPrefetch}
            onHoverEnd={hoverPrefetchCancel}
            onNavigate={navigate}
            indexStatus={indexStatus}
          />
        )}
      </main>
    </div>
  )
}

export default App
