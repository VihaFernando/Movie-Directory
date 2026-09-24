import { useEffect, useRef } from 'react'

// Keeps the browser URL in sync with the app's view/title/playback state,
// entirely via the query string on "/" - never a real path.
//
// Why the query string and not real paths (/watch/vidbox/movie/123): the
// backend serves the built frontend via FastAPI's StaticFiles(html=True)
// mounted at "/" (see app/main.py), which has no SPA fallback - it only
// resolves an exact file or "/" itself to index.html. A real path like
// /watch/... would 404 on refresh unless the backend also learned to answer
// every such path with index.html. The query string needs no such change:
// "/?view=browse&..." already resolves to "/" as far as StaticFiles is
// concerned, so this works in dev and prod with zero backend edits.
//
// This hook only reads/writes the URL - it owns no state itself. App.jsx
// still owns nav/openTitle/player exactly as before; this just mirrors them
// out to history.pushState and, once on mount, reads them back in.
export function encodeUrl({ nav, openTitle, player }) {
  const params = new URLSearchParams()

  if (openTitle) {
    // vidbox is the only enabled source today (see App.jsx SOURCE), but the
    // scheme carries it explicitly so a URL someone saved keeps working if
    // that ever changes.
    params.set('title', `vidbox:${openTitle.content_type}:${openTitle.slug}`)
    if (player.status !== 'idle') {
      params.set('play', '1')
      if (player.season != null) params.set('s', String(player.season))
      if (player.episode != null) params.set('e', String(player.episode))
    }
  } else if (nav.view === 'search') {
    // The query text itself lives in App's `query` state, not `nav` - see
    // the second param this function's caller passes for it.
  } else if (nav.view === 'browse') {
    params.set('view', 'browse')
    params.set('type', nav.contentType)
    if (nav.genre) params.set('genre', nav.genre)
    if (nav.sort && nav.sort !== 'popular') params.set('sort', nav.sort)
  } else if (nav.view === 'admin') {
    params.set('view', 'admin')
  } else if (nav.view === 'favorites') {
    params.set('view', 'favorites')
  }

  return params
}

// The same shape App.jsx's HOME constant uses. decodeUrl() always returns a
// `nav` with every one of these fields set (never a partial object) - other
// code (Navbar, BrowsePage's onNavigate spreads) reads nav.contentType even
// while on the search or home view, so a partial nav would leave that
// `undefined` rather than falling back to something sane.
const NAV_DEFAULTS = { view: 'home', contentType: 'movie', genre: '', sort: 'popular' }

// Reconstructs {nav, openTitle, wantsPlay, season, episode, query} from the
// current location - the inverse of encodeUrl(), used once on mount and
// again on every popstate (Back/Forward) to restore state from the URL.
export function decodeUrl() {
  const params = new URLSearchParams(window.location.search)
  const titleParam = params.get('title')
  const base = { openTitle: null, wantsPlay: false, season: null, episode: null, query: '' }

  if (titleParam) {
    const [source, contentType, ...slugParts] = titleParam.split(':')
    const slug = slugParts.join(':') // slugs are numeric in practice, but join defensively
    if (source && contentType && slug) {
      return {
        ...base,
        openTitle: { slug, content_type: contentType, poster_url: '' },
        wantsPlay: params.get('play') === '1',
        season: params.has('s') ? Number(params.get('s')) : null,
        episode: params.has('e') ? Number(params.get('e')) : null,
        nav: { ...NAV_DEFAULTS, contentType: contentType === 'tv' ? 'tv' : 'movie' },
      }
    }
  }

  if (params.get('view') === 'search' && params.get('q')) {
    return {
      ...base,
      query: params.get('q'),
      nav: { ...NAV_DEFAULTS, view: 'search', contentType: params.get('type') || 'movie' },
    }
  }

  if (params.get('view') === 'browse') {
    return {
      ...base,
      nav: {
        ...NAV_DEFAULTS,
        view: 'browse',
        contentType: params.get('type') || 'movie',
        genre: params.get('genre') || '',
        sort: params.get('sort') || 'popular',
      },
    }
  }

  if (params.get('view') === 'admin') {
    return { ...base, nav: { ...NAV_DEFAULTS, view: 'admin' } }
  }

  if (params.get('view') === 'favorites') {
    return { ...base, nav: { ...NAV_DEFAULTS, view: 'favorites' } }
  }

  return { ...base, nav: NAV_DEFAULTS }
}

// Pushes a new URL for the given state, replacing rather than pushing a new
// history entry when only playback progressed within the same title (e.g.
// switching episodes) - otherwise Back would have to be pressed once per
// episode instead of leaving the title/browse view in one step.
export function useUrlSync({ nav, openTitle, player, query }) {
  const lastSearchRef = useRef(null)

  useEffect(() => {
    const params = encodeUrl({ nav, openTitle, player })
    if (nav.view === 'search' && !openTitle && query.trim()) {
      params.set('view', 'search')
      params.set('q', query.trim())
    }

    const search = params.toString()
    const url = search ? `/?${search}` : '/'
    if (search === lastSearchRef.current) return

    // Replace (not push) when moving between episodes of the SAME title, or
    // when the search query is still being typed - both are refinements of
    // where the user already is, not a new place to come Back to.
    const prevParams = new URLSearchParams(lastSearchRef.current || '')
    const sameTitle = openTitle && prevParams.get('title') === params.get('title')
    const bothSearch = nav.view === 'search' && prevParams.get('view') === 'search'
    const method = lastSearchRef.current !== null && (sameTitle || bothSearch) ? 'replaceState' : 'pushState'

    window.history[method](null, '', url)
    lastSearchRef.current = search
    // `player` itself is a new object every render (usePlayer() is not
    // memoized) - depending on it directly would re-run this on every
    // render. The three fields actually read here are what should trigger
    // a sync, and they're already listed individually below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav, openTitle, player.status, player.season, player.episode, query])
}
