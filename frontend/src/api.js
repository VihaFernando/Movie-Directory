// Thin client for the FastAPI backend (see app/main.py). In dev, Vite
// proxies /api to http://127.0.0.1:8000 (see vite.config.js); in the built
// app, FastAPI serves this same origin, so no base URL is needed either way.

async function getJson(url, options) {
  const res = await fetch(url, options)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

// --- Catalog index ------------------------------------------------------
// These read the indexed catalog: the source's own listings, crawled once
// and enriched with TMDB metadata (see app/catalog_index.py). Everything
// they return is a title the source actually has, which is why browsing by
// genre here never surfaces something that cannot be played.

export function fetchBrowse({
  contentType = 'movie',
  genre = '',
  sort = 'popular',
  page = 1,
  pageSize = 20,
  source = 'vidbox',
} = {}) {
  const params = new URLSearchParams({
    content_type: contentType,
    sort,
    page: String(page),
    page_size: String(pageSize),
    source,
  })
  if (genre) params.set('genre', genre)
  return getJson(`/api/browse?${params}`)
}

export function fetchGenres(contentType = 'movie', source = 'vidbox') {
  const params = new URLSearchParams({ content_type: contentType, source })
  return getJson(`/api/genres?${params}`)
}

export function fetchIndexStatus() {
  return getJson('/api/index_status')
}

// --- Live source listings ----------------------------------------------

export function fetchCatalog(source = 'vidbox', contentType = 'movie', query = '', page = 1) {
  const params = new URLSearchParams({ source, content_type: contentType, page: String(page) })
  // A query goes to the SOURCE's own search endpoint rather than filtering
  // whatever happens to be loaded, so results cover its whole library.
  if (query.trim()) params.set('query', query.trim())
  return getJson(`/api/catalog?${params}`)
}

// Everything the detail page renders for one title: hero metadata for both
// movies and series, plus - for a series - its seasons with `season`'s
// episodes filled in. Served from TMDB where possible (fast), falling back
// to scraping the source.
export function fetchDetail(slug, source = 'vidbox', contentType = 'tv', season = null) {
  const params = new URLSearchParams({ source, content_type: contentType })
  if (season != null) params.set('season', String(season))
  return getJson(`/api/detail/${encodeURIComponent(slug)}?${params}`)
}

// --- Playback -----------------------------------------------------------

export function fetchEmbed(slug, source = 'vidbox', contentType = 'movie', season = null, episode = null) {
  const params = new URLSearchParams({ source, content_type: contentType })
  // Season/episode identify WHICH stream to resolve for a series - without
  // them the backend resolves whatever the source opens on (its pilot).
  if (contentType === 'tv' && season != null) params.set('season', String(season))
  if (contentType === 'tv' && episode != null) params.set('episode', String(episode))
  return getJson(`/api/get_embed/${encodeURIComponent(slug)}?${params}`)
}

export async function fetchSubtitles(slug, contentType = 'movie', season = null, episode = null) {
  try {
    const params = new URLSearchParams({ content_type: contentType })
    if (contentType === 'tv' && season != null) params.set('season', String(season))
    if (contentType === 'tv' && episode != null) params.set('episode', String(episode))
    const data = await getJson(`/api/subtitles/${encodeURIComponent(slug)}?${params}`)
    return data.tracks || []
  } catch {
    return [] // no subtitles available is not an error condition
  }
}

export function prefetchEmbed(slug, source = 'vidbox', contentType = 'movie', season = null, episode = null) {
  // Fire-and-forget - errors here don't matter, fetchEmbed() surfaces them
  // properly if/when the user actually opens the title.
  const params = new URLSearchParams({ source, content_type: contentType })
  if (contentType === 'tv' && season != null) params.set('season', String(season))
  if (contentType === 'tv' && episode != null) params.set('episode', String(episode))
  fetch(`/api/prefetch_embed/${encodeURIComponent(slug)}?${params}`, { method: 'POST' }).catch(() => {})
}

export function proxiedImageUrl(originalUrl) {
  if (!originalUrl) return ''
  return `/api/proxy_image?url=${encodeURIComponent(originalUrl)}`
}

// --- Favorites ------------------------------------------------------------
// Every call needs a Clerk session token (same pattern as the admin calls
// below) since favorites are per-user.

async function authedJson(token, url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: { ...options.headers, Authorization: `Bearer ${token}` },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.status === 204 ? null : res.json()
}

export function fetchFavorites(token) {
  return authedJson(token, '/api/favorites')
}

export function fetchFavoriteKeys(token) {
  return authedJson(token, '/api/favorites/keys')
}

export function addFavorite(token, item) {
  return authedJson(token, '/api/favorites', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      slug: item.slug,
      source: item.source || 'vidbox',
      content_type: item.content_type,
      title: item.title || '',
      poster_url: item.poster_url || '',
      year: item.year || '',
      rating: item.rating ?? null,
    }),
  })
}

export function removeFavorite(token, { slug, source = 'vidbox', content_type: contentType }) {
  return authedJson(
    token,
    `/api/favorites/${encodeURIComponent(source)}/${encodeURIComponent(contentType)}/${encodeURIComponent(slug)}`,
    { method: 'DELETE' },
  )
}

// --- Watch history ---------------------------------------------------------
// Same auth pattern as favorites: every call needs a Clerk session token
// since history is per-user.

export function fetchHistory(token, limit = 25) {
  return authedJson(token, `/api/history?limit=${limit}`)
}

export function saveWatchProgress(token, entry) {
  return authedJson(token, '/api/history', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      slug: entry.slug,
      source: entry.source || 'vidbox',
      content_type: entry.content_type,
      title: entry.title || '',
      poster_url: entry.poster_url || '',
      backdrop_url: entry.backdrop_url || '',
      year: entry.year || '',
      season: entry.season ?? null,
      episode: entry.episode ?? null,
      progress_seconds: entry.progress_seconds || 0,
      duration_seconds: entry.duration_seconds || 0,
    }),
  })
}

export function removeWatchHistory(token, { slug, source = 'vidbox', content_type: contentType }) {
  return authedJson(
    token,
    `/api/history/${encodeURIComponent(source)}/${encodeURIComponent(contentType)}/${encodeURIComponent(slug)}`,
    { method: 'DELETE' },
  )
}

// --- Admin ---------------------------------------------------------------
// Every call here needs a Clerk session token, which only a component (via
// useAuth().getToken()) can produce - so each function takes the token as
// its first argument rather than fetching it itself.

async function adminJson(token, url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: { ...options.headers, Authorization: `Bearer ${token}` },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

export function fetchAdminMe(token) {
  return adminJson(token, '/api/admin/me')
}

export function fetchAdminUsers(token, { limit = 50, offset = 0, query = '' } = {}) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (query.trim()) params.set('query', query.trim())
  return adminJson(token, `/api/admin/users?${params}`)
}

export function setAdminUserRole(token, userId, role) {
  return adminJson(token, `/api/admin/users/${encodeURIComponent(userId)}/role`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  })
}

export function banAdminUser(token, userId) {
  return adminJson(token, `/api/admin/users/${encodeURIComponent(userId)}/ban`, { method: 'POST' })
}

export function unbanAdminUser(token, userId) {
  return adminJson(token, `/api/admin/users/${encodeURIComponent(userId)}/unban`, { method: 'POST' })
}

export function deleteAdminUser(token, userId) {
  return adminJson(token, `/api/admin/users/${encodeURIComponent(userId)}`, { method: 'DELETE' })
}

export function fetchAdminAuditLog(token, { limit = 50 } = {}) {
  return adminJson(token, `/api/admin/audit-log?limit=${limit}`)
}
