import { ArrowLeft, Play, Star } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { fetchBrowse, proxiedImageUrl } from '../api'
import { MovieCard } from '../components/MovieCard'
import { VideoPlayer } from '../components/VideoPlayer'
import { useWatchHistory } from '../hooks/useWatchHistory.jsx'
import { cn } from '../lib/utils'

// Watch page: a proper page rather than a modal overlay. The player sits
// large in the top-left; the title's own details run underneath it, and the
// right rail carries either similar titles (movies) or the current season's
// episode list (series) - whichever is more useful while something is
// already playing.
//
// Clicking the player's own fullscreen button is untouched: VideoPlayer
// requests fullscreen on its own .vp-container, so this layout only governs
// the non-fullscreen, "watching inline" state.
export function WatchPage({
  player,
  detail: detailState,
  contentType,
  onBack,
  episodes,
  seasons,
  selectedSeason,
  selectSeason,
  onPlayEpisode,
  onOpenSuggestion,
}) {
  const { status, error, embed, posterUrl, season, episode, isRetry, videoRef } = player
  const isSeries = contentType === 'tv'
  const detail = detailState.detail
  // A retry re-resolves the SAME title that was already on screen (a stale
  // link, a flaky capture) rather than opening a different one, so the
  // player stays mounted through it and shows its own spinner - unmounting
  // it here would tear the fullscreen element out from under the browser if
  // fullscreen was active, which broke fullscreen's controls specifically on
  // titles whose capture retries often (see usePlayer.js's isRetry).
  const keepPlayerMounted = status === 'ready' || (status === 'loading' && isRetry)

  const { saveProgress } = useWatchHistory()
  // Checkpoint playback position periodically (throttled inside
  // saveProgress itself) so "Continue Watching" can resume close to where
  // the viewer actually stopped, not just at natural pause points. Reads
  // the video element directly via videoRef rather than tracking
  // currentTime in React state - this only needs to run every few seconds,
  // so there's no reason to re-render the whole page every timeupdate.
  const detailRef = useRef(detail)
  detailRef.current = detail
  // The last REAL playhead position seen while actually playing, tracked
  // continuously rather than read from the element at save time. This
  // mirrors usePlayer.js's own lastTimeRef and exists for the same reason:
  // by the time this effect's cleanup runs (leaving the page, or React
  // tearing the <video> down on unmount), the element can already report
  // currentTime as 0 - saving that literal value on exit was overwriting a
  // real position with "just started", which the backend correctly reads as
  // "not really watched" and deletes (see MIN_PROGRESS_SECONDS in
  // app/watch_history.py). That looked like history vanishing on its own
  // whenever the watch page unmounted, including on a simple back-navigation
  // to Home.
  const lastPositionRef = useRef(0)
  const lastDurationRef = useRef(0)
  useEffect(() => {
    // Only HLS is actually attached to the <video> element today (see
    // usePlayer.js's MANIFEST_PARSED-gated effect) - a direct-media embed
    // has no <video src> wired up at all yet, so there's nothing playing to
    // checkpoint in that case.
    if (status !== 'ready' || !embed?.is_hls) return
    const video = videoRef.current
    if (!video) return

    const checkpoint = (opts, { fromCache = false } = {}) => {
      const d = detailRef.current
      if (!d) return
      const position = fromCache ? lastPositionRef.current : video.currentTime
      const duration = fromCache ? lastDurationRef.current : video.duration || 0
      if (!Number.isFinite(position)) return
      saveProgress(
        {
          slug: d.slug,
          source: d.source,
          content_type: contentType,
          title: d.title,
          poster_url: d.poster_url,
          backdrop_url: d.backdrop_url,
          year: d.year,
          season,
          episode,
          progress_seconds: position,
          duration_seconds: duration,
        },
        opts,
      )
    }

    const onTime = () => {
      // Only a genuinely advancing position (not 0, not a reset) is worth
      // remembering as "the real progress" - see lastPositionRef above.
      if (video.currentTime > 0) {
        lastPositionRef.current = video.currentTime
        lastDurationRef.current = video.duration || 0
      }
      checkpoint()
    }
    // A pause is a natural "the viewer stopped here" moment worth saving
    // immediately rather than waiting out the throttle window.
    const onPause = () => checkpoint({ force: true })
    video.addEventListener('timeupdate', onTime)
    video.addEventListener('pause', onPause)
    return () => {
      video.removeEventListener('timeupdate', onTime)
      video.removeEventListener('pause', onPause)
      // Save once more on leaving this title/unmounting, so navigating away
      // mid-scene doesn't lose whatever progress happened since the last
      // throttled save. Uses the tracked position, not a live read of
      // `video` - see lastPositionRef's comment for why reading the element
      // itself here is what caused history to disappear.
      checkpoint({ force: true }, { fromCache: true })
    }
  }, [status, embed?.is_hls, videoRef, contentType, season, episode, saveProgress])

  return (
    <div className="mx-auto max-w-[1600px] px-4 pb-16 pt-4">
      <button
        type="button"
        onClick={onBack}
        className="mb-4 flex items-center gap-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back
      </button>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
        {/* Left/top column: player, then this title's own details */}
        <div className="min-w-0 space-y-6">
          <div className="relative aspect-video w-full overflow-hidden rounded-lg bg-black">
            {status === 'loading' && !isRetry && (
              <div className="absolute inset-0 flex items-center justify-center">
                {posterUrl && (
                  <img
                    src={posterUrl.startsWith('/api') ? posterUrl : proxiedImageUrl(posterUrl)}
                    alt=""
                    className="absolute inset-0 h-full w-full object-cover opacity-40 blur-[2px]"
                  />
                )}
                <div className="relative flex flex-col items-center gap-3 text-white">
                  <div className="vp-spinner" />
                  {season != null && episode != null
                    ? `Resolving S${season}E${episode}...`
                    : 'Resolving stream...'}
                </div>
              </div>
            )}

            {status === 'error' && (
              <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-white/80">
                Failed to load video: {error}
              </div>
            )}

            {/* Rendered through a retry's 'loading' phase too (not just
                'ready'), and internally shows its own spinner while embed is
                stale/missing - see keepPlayerMounted above for why this
                can't just unmount on every 'loading'. */}
            {keepPlayerMounted && (status !== 'ready' || embed?.is_media) && (
              <VideoPlayer
                player={player}
                posterUrl={posterUrl}
                title={detail?.title}
                subtitle={isSeries && season != null ? `S${season}E${episode} · ${detail?.title || ''}` : null}
              />
            )}

            {status === 'ready' && !embed?.is_media && (
              <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-white/80">
                This source provides an HTML player with third-party ad and plugin code. It was
                blocked to keep playback local and ad-free.
              </div>
            )}
          </div>

          <TitleDetails
            detail={detail}
            isSeries={isSeries}
            seasons={seasons}
            selectedSeason={selectedSeason}
            selectSeason={selectSeason}
          />
        </div>

        {/* Right column: episodes for a series, otherwise similar titles */}
        <div className="min-w-0">
          {isSeries ? (
            <EpisodeRail
              episodes={episodes}
              currentSeason={season}
              currentEpisode={episode}
              onPlayEpisode={onPlayEpisode}
              status={detailState.status}
            />
          ) : (
            <SuggestedRail
              genre={detail?.genres?.[0]}
              title={detail?.title}
              excludeSlug={detail?.slug}
              onOpen={onOpenSuggestion}
            />
          )}
        </div>
      </div>
    </div>
  )
}

function TitleDetails({ detail, isSeries, seasons, selectedSeason, selectSeason }) {
  if (!detail) return null

  return (
    <section className="space-y-3 border-t border-border pt-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold leading-tight sm:text-3xl">{detail.title}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
            <span className="uppercase">{isSeries ? 'TV' : 'Movie'}</span>
            {detail.rating != null && (
              <span className="flex items-center gap-1 font-semibold text-yellow-400">
                <Star className="h-4 w-4 fill-yellow-400" />
                {detail.rating.toFixed(1)}
              </span>
            )}
            {detail.year && <span>{detail.year}</span>}
          </div>
        </div>

        {isSeries && seasons.length > 0 && (
          <select
            value={selectedSeason ?? ''}
            onChange={(e) => selectSeason(Number(e.target.value))}
            aria-label="Season"
            className="h-10 shrink-0 rounded-md border border-border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-ring"
          >
            {seasons.map((s) => (
              <option key={s.season_number} value={s.season_number}>
                {s.name}
              </option>
            ))}
          </select>
        )}
      </div>

      {detail.genres?.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {detail.genres.map((genre) => (
            <span
              key={genre}
              className="rounded-full border border-primary/40 bg-primary/15 px-3 py-1 text-xs text-foreground/90"
            >
              {genre}
            </span>
          ))}
        </div>
      )}

      {isSeries && seasons.length > 0 && selectedSeason != null && (
        <p className="text-sm text-muted-foreground">
          {seasons.find((s) => s.season_number === selectedSeason)?.overview || detail.overview}
        </p>
      )}
      {!isSeries && detail.overview && (
        <p className="text-sm leading-relaxed text-foreground/90">{detail.overview}</p>
      )}
    </section>
  )
}

function EpisodeRail({ episodes, currentSeason, currentEpisode, onPlayEpisode, status }) {
  return (
    <div className="lg:sticky lg:top-20">
      <h2 className="mb-3 text-lg font-bold">Episodes</h2>

      {status === 'refreshing' && <p className="py-3 text-sm text-muted-foreground">Loading season…</p>}
      {status === 'ready' && episodes.length === 0 && (
        <p className="py-3 text-sm text-muted-foreground">No episodes listed for this season.</p>
      )}

      <ul className="no-scrollbar flex max-h-[75vh] flex-col gap-2 overflow-y-auto pr-1">
        {episodes.map((ep) => {
          const isCurrent = currentSeason === ep.season_number && currentEpisode === ep.episode_number
          return (
            <li key={`${ep.season_number}-${ep.episode_number}`}>
              <button
                type="button"
                onClick={() => onPlayEpisode(ep.season_number, ep.episode_number)}
                className={cn(
                  'group flex w-full gap-3 rounded-lg border p-2 text-left transition-colors',
                  isCurrent
                    ? 'border-primary/60 bg-card'
                    : 'border-transparent bg-card/60 hover:border-border hover:bg-card',
                )}
              >
                <div className="relative aspect-video w-28 shrink-0 overflow-hidden rounded-md bg-muted">
                  {ep.still_url && (
                    <img
                      src={proxiedImageUrl(ep.still_url)}
                      alt=""
                      loading="lazy"
                      className="h-full w-full object-cover"
                    />
                  )}
                  <span className="absolute left-0 top-0 rounded-br-md bg-black/75 px-1.5 py-0.5 text-[11px] font-bold">
                    {ep.episode_number}
                  </span>
                  <span className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100">
                    <Play className="h-5 w-5 fill-white text-white" />
                  </span>
                </div>
                <div className="min-w-0 flex-1 self-center">
                  <h3 className="line-clamp-1 text-sm font-semibold">{ep.title}</h3>
                  {ep.overview && (
                    <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
                      {ep.overview}
                    </p>
                  )}
                </div>
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// A title's first "word" for prefix matching - e.g. "Spider-Man: Brand New
// Day" -> "spider" - so "Spider-Man", "Spider-Man 2" etc. group together
// even though hyphens/colons split them differently. Short (<3 char) leading
// words ("A", "The") are skipped since they'd match almost everything.
function titlePrefix(title) {
  const word = (title || '').toLowerCase().match(/[a-z0-9]+/)?.[0] || ''
  return word.length >= 3 ? word : ''
}

function SuggestedRail({ genre, title, excludeSlug, onOpen }) {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchBrowse({ contentType: 'movie', genre: genre || '', sort: genre ? 'top_rated' : 'trending', pageSize: 12 })
      .then((data) => !cancelled && setItems((data.items || []).filter((it) => it.slug !== excludeSlug)))
      .catch(() => !cancelled && setItems([]))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [genre, excludeSlug])

  if (!loading && items.length === 0) return null

  // Same-franchise titles ("Spider-Man...") first, if any are actually in
  // this pool - otherwise the plain genre/trending order is left untouched.
  const prefix = titlePrefix(title)
  const sorted = prefix
    ? [...items].sort((a, b) => {
        const aMatch = titlePrefix(a.title) === prefix
        const bMatch = titlePrefix(b.title) === prefix
        return aMatch === bMatch ? 0 : aMatch ? -1 : 1
      })
    : items

  return (
    <div className="lg:sticky lg:top-20">
      <h2 className="mb-3 text-lg font-bold">{genre ? `More ${genre}` : 'You may also like'}</h2>
      <div className="no-scrollbar grid max-h-[75vh] grid-cols-2 gap-3 overflow-y-auto pr-1 sm:grid-cols-3 lg:grid-cols-2">
        {loading
          ? Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="aspect-[2/3] animate-pulse rounded-lg bg-muted" />
            ))
          : sorted.map((item) => (
              <MovieCard key={`${item.content_type}-${item.slug}`} item={item} onOpen={onOpen} />
            ))}
      </div>
    </div>
  )
}
