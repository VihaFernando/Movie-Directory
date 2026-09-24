import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchDetail } from '../api'

// Loads the detail page for one title: hero metadata for movies and series
// alike, plus the selected season's episodes for a series.
//
// The backend fills in episodes for one season at a time (the source only
// renders one season's list at a time - see app/scraper_series.py), so
// changing season here means another request rather than a local filter.
// Seasons already fetched are kept in `episodesBySeason` so switching back
// to one is instant instead of re-scraping it.
export function useDetail(slug, source, contentType) {
  const active = Boolean(slug)

  const [status, setStatus] = useState(active ? 'loading' : 'idle')
  const [error, setError] = useState(null)
  const [detail, setDetail] = useState(null)
  const [selectedSeason, setSelectedSeason] = useState(null)
  const [episodesBySeason, setEpisodesBySeason] = useState({})

  // Identifies which title/season a response belongs to, so a slow reply
  // for something the user has already navigated away from is discarded
  // rather than overwriting what is now on screen.
  const requestKey = `${source}|${contentType}|${slug}|${selectedSeason ?? 'default'}`
  const activeKeyRef = useRef(requestKey)

  // Reset when the title itself changes - keeping the previous one's
  // seasons would briefly render its episodes under the new title.
  useEffect(() => {
    setDetail(null)
    setEpisodesBySeason({})
    setSelectedSeason(null)
    setError(null)
    setStatus(active ? 'loading' : 'idle')
  }, [slug, source, contentType, active])

  // Read the episode cache inside the effect through a ref, so the effect
  // does not depend on the very state it writes - that would re-run it on
  // its own result and refetch forever.
  const cacheRef = useRef(episodesBySeason)
  useEffect(() => {
    cacheRef.current = episodesBySeason
  }, [episodesBySeason])

  useEffect(() => {
    if (!active) return
    // Already have this season's episodes - no need to ask again.
    if (selectedSeason != null && cacheRef.current[selectedSeason]) {
      setStatus('ready')
      return
    }

    const keyAtRequest = requestKey
    activeKeyRef.current = keyAtRequest
    let cancelled = false
    setStatus((s) => (s === 'ready' ? 'refreshing' : 'loading'))

    fetchDetail(slug, source, contentType, selectedSeason)
      .then((data) => {
        if (cancelled || activeKeyRef.current !== keyAtRequest) return
        setDetail(data)
        const filled = (data.seasons || []).find((s) => (s.episodes || []).length > 0)
        if (filled) {
          setEpisodesBySeason((prev) => ({ ...prev, [filled.season_number]: filled.episodes }))
        }
        // Adopt the backend's chosen season on the first load, so the UI
        // agrees with the episodes it actually returned (it skips a
        // "Specials" season 0 rather than opening on it).
        if (selectedSeason == null && data.selected_season != null) {
          setSelectedSeason(data.selected_season)
        }
        setStatus('ready')
        setError(null)
      })
      .catch((err) => {
        if (cancelled || activeKeyRef.current !== keyAtRequest) return
        setError(err.message)
        setStatus('error')
      })

    return () => {
      cancelled = true
    }
  }, [active, slug, source, contentType, selectedSeason, requestKey])

  const selectSeason = useCallback((seasonNumber) => {
    setSelectedSeason(seasonNumber)
  }, [])

  const seasons = detail?.seasons || []
  const episodes = selectedSeason != null ? episodesBySeason[selectedSeason] || [] : []

  return { status, error, detail, seasons, selectedSeason, selectSeason, episodes }
}
