import { useAuth } from '@clerk/react'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { fetchHistory, removeWatchHistory, saveWatchProgress } from '../api'

// App-wide "Continue Watching" state, structured exactly like
// FavoritesProvider (see hooks/useFavorites.jsx) for the same reasons: one
// shared cache rather than every consumer (HomePage's row, the player's
// progress reporter) fetching independently and drifting out of sync.
const WatchHistoryContext = createContext(null)

const LIST_CACHE_PREFIX = 'history:list:'
// Same window as favorites: a page load/refresh should render instantly from
// cache, full stop, not re-hit the server just because some time passed.
// This does NOT mean history goes stale while actually watching something -
// saveProgress() below force-refreshes the list itself right after every
// successful checkpoint, independent of this timer, which is what keeps
// "Continue Watching" live during a session. This timer only matters for the
// rarer case of a stale tab left open across a long gap.
const REVALIDATE_AFTER_MS = 60_000
// How often a playing title checkpoints its progress to the server. Frequent
// enough that closing the tab rarely loses more than this much progress,
// infrequent enough not to hammer the API every render.
const SAVE_INTERVAL_MS = 15_000

function readCache(userId) {
  if (!userId) return null
  try {
    const raw = sessionStorage.getItem(LIST_CACHE_PREFIX + userId)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function writeCache(userId, data) {
  if (!userId) return
  try {
    sessionStorage.setItem(LIST_CACHE_PREFIX + userId, JSON.stringify({ data, savedAt: Date.now() }))
  } catch {
    // Storage full/unavailable - caching is an optimization only.
  }
}

function clearCache(userId) {
  if (!userId) return
  try {
    sessionStorage.removeItem(LIST_CACHE_PREFIX + userId)
  } catch {
    // ignore
  }
}

export function WatchHistoryProvider({ children }) {
  const { isLoaded, isSignedIn, userId, getToken } = useAuth()
  const [list, setList] = useState(() => [])
  const [status, setStatus] = useState('idle') // idle | loading | ready | error

  useEffect(() => {
    if (!isLoaded) return
    if (!isSignedIn || !userId) {
      setList([])
      setStatus('idle')
      return
    }
    const cached = readCache(userId)
    if (cached) {
      setList(cached.data)
      setStatus('ready')
    }
  }, [isLoaded, isSignedIn, userId])

  const refresh = useCallback(
    async (force = false) => {
      if (!isSignedIn || !userId) return
      if (!force) {
        const cached = readCache(userId)
        if (cached && Date.now() - cached.savedAt < REVALIDATE_AFTER_MS) {
          setList(cached.data)
          setStatus('ready')
          return
        }
      }
      setStatus((s) => (s === 'ready' ? 'ready' : 'loading'))
      try {
        const token = await getToken()
        const data = await fetchHistory(token)
        const items = data.items || []
        setList(items)
        setStatus('ready')
        writeCache(userId, items)
      } catch {
        setStatus('error')
      }
    },
    [isSignedIn, userId, getToken],
  )

  // Load automatically once signed in - "Continue Watching" needs to be
  // ready as soon as the home page mounts, not only after something
  // explicitly asks for it.
  useEffect(() => {
    if (isLoaded && isSignedIn) refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoaded, isSignedIn, userId])

  // Debounced/throttled progress reporter used by the player. Call on every
  // timeupdate-ish tick; internally it only actually hits the network at
  // most once per SAVE_INTERVAL_MS, plus once immediately for the very
  // first call for a given title (so a quick watch-then-leave still saves).
  const lastSaveRef = useRef({ key: null, at: 0 })
  const saveProgress = useCallback(
    async (entry, { force = false } = {}) => {
      if (!isSignedIn || !userId) return
      const key = `${entry.source || 'vidbox'}:${entry.content_type}:${entry.slug}`
      const now = Date.now()
      const last = lastSaveRef.current
      const isNewTitle = last.key !== key
      if (!force && !isNewTitle && now - last.at < SAVE_INTERVAL_MS) return
      lastSaveRef.current = { key, at: now }

      try {
        const token = await getToken()
        await saveWatchProgress(token, entry)
        // Invalidate AND actually re-fetch: completion (dropping below
        // MIN_PROGRESS_SECONDS or crossing COMPLETED_FRACTION) is decided
        // server-side, so the cached list can't be trusted to still match
        // reality without asking again. Re-fetching here (not just
        // invalidating the cache) is what makes a freshly-watched title
        // show up in "Continue Watching" back on Home without that page
        // needing to know a save just happened - clearing the cache alone
        // left `list` itself stale until something unrelated happened to
        // call refresh() again, which is why the row never updated after
        // watching something and navigating back.
        clearCache(userId)
        refresh(true)
      } catch {
        // A missed checkpoint just means slightly less accurate resume
        // position next time - never worth surfacing to the viewer.
      }
    },
    [isSignedIn, userId, getToken, refresh],
  )

  const removeEntry = useCallback(
    async (item) => {
      if (!isSignedIn || !userId) return
      const key = `${item.source || 'vidbox'}:${item.content_type}:${item.slug}`
      setList((prev) => prev.filter((entry) => `${entry.source}:${entry.content_type}:${entry.slug}` !== key))
      try {
        const token = await getToken()
        await removeWatchHistory(token, item)
      } finally {
        clearCache(userId)
      }
    },
    [isSignedIn, userId, getToken],
  )

  const prevUserIdRef = useRef(userId)
  useEffect(() => {
    if (prevUserIdRef.current && prevUserIdRef.current !== userId) {
      clearCache(prevUserIdRef.current)
    }
    prevUserIdRef.current = userId
  }, [userId])

  const value = useMemo(
    () => ({
      isLoaded,
      isSignedIn: Boolean(isSignedIn),
      history: list,
      historyStatus: status,
      refreshHistory: refresh,
      saveProgress,
      removeHistoryEntry: removeEntry,
    }),
    [isLoaded, isSignedIn, list, status, refresh, saveProgress, removeEntry],
  )

  return <WatchHistoryContext.Provider value={value}>{children}</WatchHistoryContext.Provider>
}

export function useWatchHistory() {
  const ctx = useContext(WatchHistoryContext)
  if (!ctx) throw new Error('useWatchHistory must be used within a WatchHistoryProvider')
  return ctx
}
