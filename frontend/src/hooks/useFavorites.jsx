import { useAuth } from '@clerk/react'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { addFavorite, fetchFavoriteKeys, fetchFavorites, removeFavorite } from '../api'

// App-wide favorites state: which titles the signed-in user has saved, kept
// as a single Set of 'source:content_type:slug' keys shared via context so
// every heart button (grid cards, the detail page, ...) reflects the same
// toggle instantly instead of each card holding its own stale copy.
//
// A Context (rather than one useFavorites() call per component) is what
// makes that sharing possible - the alternative would be N independent
// fetches all racing each other and drifting out of sync on toggle.
const FavoritesContext = createContext(null)

function favoriteKey(item) {
  return `${item.source || 'vidbox'}:${item.content_type}:${item.slug}`
}

// sessionStorage cache, one entry per Clerk user id, so a refresh (or moving
// between pages) restores the heart state instantly instead of showing every
// card as un-favorited until a fresh request lands - and so the favorites
// list itself doesn't re-hit the server on every visit to that page within
// the same tab session. Cleared naturally when the tab closes; a stale entry
// from a previous account is simply ignored since it's keyed by user id.
const KEYS_CACHE_PREFIX = 'favorites:keys:'
const LIST_CACHE_PREFIX = 'favorites:list:'
// Revalidate in the background if the cached snapshot is older than this -
// keeps the UI instant on every mount while still catching changes made in
// another tab/device within a reasonable window.
const REVALIDATE_AFTER_MS = 60_000

function readCache(prefix, userId) {
  if (!userId) return null
  try {
    const raw = sessionStorage.getItem(prefix + userId)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function writeCache(prefix, userId, data) {
  if (!userId) return
  try {
    sessionStorage.setItem(prefix + userId, JSON.stringify({ data, savedAt: Date.now() }))
  } catch {
    // Storage full/unavailable (private browsing) - caching is an
    // optimization only, so silently skip it.
  }
}

function clearUserCaches(userId) {
  if (!userId) return
  try {
    sessionStorage.removeItem(KEYS_CACHE_PREFIX + userId)
    sessionStorage.removeItem(LIST_CACHE_PREFIX + userId)
  } catch {
    // ignore
  }
}

export function FavoritesProvider({ children }) {
  // isLoaded gates every decision below on whether Clerk has actually
  // finished restoring the session from its own storage - without it,
  // isSignedIn reads as false for a frame on every refresh (Clerk hasn't
  // hydrated yet), which is what made a refresh on the Favorites page briefly
  // show "sign in" for an already-signed-in user.
  const { isLoaded, isSignedIn, userId, getToken } = useAuth()
  const [keys, setKeys] = useState(() => new Set())
  const [list, setList] = useState(() => [])
  const [keysReady, setKeysReady] = useState(false)
  const [listStatus, setListStatus] = useState('idle') // idle | loading | ready | error

  // Seed synchronously from cache the moment we know who the user is, before
  // any network round-trip - this is what makes a refresh show correct
  // hearts immediately instead of flashing "unfavorited" for a beat.
  useEffect(() => {
    if (!isLoaded) return
    if (!isSignedIn || !userId) {
      setKeys(new Set())
      setList([])
      setKeysReady(false)
      setListStatus('idle')
      return
    }
    const cachedKeys = readCache(KEYS_CACHE_PREFIX, userId)
    if (cachedKeys) {
      setKeys(new Set(cachedKeys.data))
      setKeysReady(true)
    }
    const cachedList = readCache(LIST_CACHE_PREFIX, userId)
    if (cachedList) {
      setList(cachedList.data)
      setListStatus('ready')
    }
  }, [isLoaded, isSignedIn, userId])

  // Fetch (or silently revalidate) the key set once Clerk is hydrated and
  // signed in. Runs immediately on a cache miss; on a cache hit it still
  // revalidates in the background once the cached snapshot is stale, so
  // long-lived tabs don't drift from what another device changed.
  useEffect(() => {
    if (!isLoaded || !isSignedIn || !userId) return
    const cached = readCache(KEYS_CACHE_PREFIX, userId)
    const isStale = !cached || Date.now() - cached.savedAt > REVALIDATE_AFTER_MS
    if (!isStale) return

    let cancelled = false
    getToken()
      .then((token) => fetchFavoriteKeys(token))
      .then((data) => {
        if (cancelled) return
        const nextKeys = data.keys || []
        setKeys(new Set(nextKeys))
        setKeysReady(true)
        writeCache(KEYS_CACHE_PREFIX, userId, nextKeys)
      })
      .catch(() => {
        // A failed revalidation keeps whatever was already shown (cached or
        // empty) rather than clearing state the user can currently see.
        if (!cancelled) setKeysReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [isLoaded, isSignedIn, userId, getToken])

  const isFavorite = useCallback((item) => keys.has(favoriteKey(item)), [keys])

  const toggleFavorite = useCallback(
    async (item) => {
      if (!isSignedIn || !userId) return
      const key = favoriteKey(item)
      const wasFavorite = keys.has(key)

      // Optimistic: flip immediately so the heart responds on click, then
      // reconcile with the server. A failure rolls the Set back rather than
      // leaving the UI claiming a state the backend never saved.
      const nextKeysArray = []
      setKeys((prev) => {
        const next = new Set(prev)
        if (wasFavorite) next.delete(key)
        else next.add(key)
        nextKeysArray.push(...next)
        return next
      })
      writeCache(KEYS_CACHE_PREFIX, userId, nextKeysArray)
      // The favorites list itself is now stale (an item was added/removed) -
      // drop its cache so the next visit to the Favorites page refetches
      // rather than showing a list missing/containing the just-toggled item.
      try {
        sessionStorage.removeItem(LIST_CACHE_PREFIX + userId)
      } catch {
        // ignore
      }
      setListStatus((s) => (s === 'ready' ? 'idle' : s))

      try {
        const token = await getToken()
        if (wasFavorite) {
          await removeFavorite(token, item)
        } else {
          await addFavorite(token, item)
        }
      } catch {
        setKeys((prev) => {
          const next = new Set(prev)
          if (wasFavorite) next.add(key)
          else next.delete(key)
          writeCache(KEYS_CACHE_PREFIX, userId, [...next])
          return next
        })
      }
    },
    [isSignedIn, userId, keys, getToken],
  )

  // Lazily loads the full favorites list (title/poster/rating, not just
  // keys) for the Favorites page, reusing the sessionStorage cache the same
  // way the key set does. Exposed as a function rather than fetched eagerly
  // here, since most sessions never visit that page at all.
  const loadListRef = useRef(null)
  loadListRef.current = useCallback(
    async (force = false) => {
      if (!isSignedIn || !userId) return
      if (!force) {
        const cached = readCache(LIST_CACHE_PREFIX, userId)
        if (cached && Date.now() - cached.savedAt < REVALIDATE_AFTER_MS) {
          setList(cached.data)
          setListStatus('ready')
          return
        }
      }
      setListStatus((s) => (s === 'ready' && !force ? 'ready' : 'loading'))
      try {
        const token = await getToken()
        const data = await fetchFavorites(token)
        const items = data.items || []
        setList(items)
        setListStatus('ready')
        writeCache(LIST_CACHE_PREFIX, userId, items)
      } catch {
        setListStatus('error')
      }
    },
    [isSignedIn, userId, getToken],
  )
  const ensureListLoaded = useCallback((force) => loadListRef.current(force), [])

  // Sign-out must not leak the previous account's favorites into the next
  // sign-in on the same tab.
  const prevUserIdRef = useRef(userId)
  useEffect(() => {
    if (prevUserIdRef.current && prevUserIdRef.current !== userId) {
      clearUserCaches(prevUserIdRef.current)
    }
    prevUserIdRef.current = userId
  }, [userId])

  const value = useMemo(
    () => ({
      isLoaded,
      isSignedIn: Boolean(isSignedIn),
      isFavorite,
      toggleFavorite,
      keysReady,
      favoritesList: list,
      favoritesListStatus: listStatus,
      ensureListLoaded,
    }),
    [isLoaded, isSignedIn, isFavorite, toggleFavorite, keysReady, list, listStatus, ensureListLoaded],
  )

  return <FavoritesContext.Provider value={value}>{children}</FavoritesContext.Provider>
}

export function useFavorites() {
  const ctx = useContext(FavoritesContext)
  if (!ctx) throw new Error('useFavorites must be used within a FavoritesProvider')
  return ctx
}
