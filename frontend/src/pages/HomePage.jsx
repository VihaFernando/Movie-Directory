import { Clock, Flame, RotateCcw, Star, TrendingUp, Tv } from 'lucide-react'
import { Fragment, useEffect, useState } from 'react'
import { fetchBrowse, proxiedImageUrl } from '../api'
import { MovieRow } from '../components/MovieRow'
import { Button } from '../components/ui/Button'
import { Skeleton } from '../components/ui/Skeleton'
import { useWatchHistory } from '../hooks/useWatchHistory.jsx'

// Home page, ported from the MovieFlix layout: a featured hero followed by
// horizontally scrolling shelves.
//
// Every shelf reads /api/browse, which serves the INDEXED SOURCE CATALOG -
// not TMDB discovery. That is the difference that matters: MovieFlix's rows
// came from TMDB's global library, most of which would not be playable
// here. These rows only contain titles the source actually has.
const ROWS = [
  { key: 'trending', title: 'Trending Now', icon: TrendingUp, params: { contentType: 'movie', sort: 'trending' } },
  { key: 'top_movies', title: 'Top Rated Movies', icon: Star, params: { contentType: 'movie', sort: 'top_rated' } },
  { key: 'top_tv', title: 'Top Rated TV Shows', icon: Tv, params: { contentType: 'tv', sort: 'top_rated' } },
  { key: 'new', title: 'Recently Released', icon: Clock, params: { contentType: 'movie', sort: 'newest' } },
  { key: 'action', title: 'Action & Adventure', icon: Flame, params: { contentType: 'movie', genre: 'Action', sort: 'top_rated' } },
  { key: 'drama', title: 'Drama', icon: Flame, params: { contentType: 'movie', genre: 'Drama', sort: 'top_rated' } },
  { key: 'comedy', title: 'Comedy', icon: Flame, params: { contentType: 'movie', genre: 'Comedy', sort: 'top_rated' } },
]

export function HomePage({ onOpen, onResume, onHover, onHoverEnd, onNavigate, indexStatus }) {
  const [rows, setRows] = useState({})
  const [loading, setLoading] = useState(true)
  const { isSignedIn, history } = useWatchHistory()

  useEffect(() => {
    let cancelled = false
    setLoading(true)

    Promise.all(
      ROWS.map((row) =>
        fetchBrowse({ ...row.params, pageSize: 18 })
          .then((data) => [row.key, data.items || []])
          // One failed shelf must not blank the whole page.
          .catch(() => [row.key, []]),
      ),
    ).then((entries) => {
      if (cancelled) return
      setRows(Object.fromEntries(entries))
      setLoading(false)
    })

    return () => {
      cancelled = true
    }
    // Re-run as the background crawl fills the index, so shelves populate
    // instead of staying empty on a cold start.
  }, [indexStatus?.status, indexStatus?.total])

  // Feature the highest-rated indexed title. There is no editorial
  // "featured" concept in the source, so this is the honest stand-in
  // rather than an invented one.
  const featured = rows.trending?.[0]

  return (
    <div className="space-y-8 pb-16">
      {loading && !featured ? (
        <Skeleton className="h-[420px] w-full rounded-none sm:h-[520px]" />
      ) : (
        featured && <Hero item={featured} onOpen={onOpen} />
      )}

      <div className="mx-auto max-w-[1400px] space-y-8 px-4">
        {indexStatus?.status === 'building' && (
          <p className="rounded-md border border-border bg-card px-4 py-3 text-sm text-muted-foreground">
            Building the catalog from the source… {indexStatus.total} titles so far. Rows fill in as
            it goes.
          </p>
        )}

        {ROWS.map((row, index) => (
          <Fragment key={row.key}>
            <MovieRow
              title={row.title}
              icon={row.icon}
              items={rows[row.key] || []}
              loading={loading}
              onOpen={onOpen}
              onHover={onHover}
              onHoverEnd={onHoverEnd}
              onSeeAll={() =>
                onNavigate({
                  view: 'browse',
                  contentType: row.params.contentType,
                  genre: row.params.genre || '',
                  sort: row.params.sort,
                })
              }
            />
            {/* Right after "Trending Now" (the first row) - resuming
                something already started is the most relevant thing to
                offer once the viewer has seen what's trending. */}
            {index === 0 && isSignedIn && history.length > 0 && (
              <MovieRow
                title="Continue Watching"
                icon={RotateCcw}
                items={history}
                onOpen={onResume}
              />
            )}
          </Fragment>
        ))}
      </div>
    </div>
  )
}

function Hero({ item, onOpen }) {
  return (
    <section className="relative isolate flex min-h-[420px] items-end overflow-hidden sm:min-h-[520px]">
      {item.backdrop_url && (
        <img
          src={proxiedImageUrl(item.backdrop_url)}
          alt=""
          className="absolute inset-0 z-0 h-full w-full object-cover object-[center_20%]"
        />
      )}
      {/* Two scrims: bottom-up blends the art into the page, left-side keeps
          the copy legible over whatever the image happens to contain. */}
      <div className="absolute inset-0 z-10 bg-gradient-to-t from-background via-background/60 to-background/10" />
      <div className="absolute inset-0 z-10 bg-gradient-to-r from-background/90 via-background/40 to-transparent" />

      <div className="relative z-20 mx-auto w-full max-w-[1400px] px-4 pb-10 pt-24">
        <div className="max-w-2xl">
          <h1 className="text-3xl font-bold leading-tight sm:text-5xl">{item.title}</h1>

          <div className="mt-3 flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
            <span className="uppercase">{item.content_type === 'tv' ? 'TV' : 'Movie'}</span>
            {item.rating != null && (
              <span className="flex items-center gap-1 font-semibold text-yellow-400">
                <Star className="h-4 w-4 fill-yellow-400" />
                {item.rating.toFixed(1)}
              </span>
            )}
            {item.year && <span>{item.year}</span>}
          </div>

          {item.genres?.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {item.genres.slice(0, 3).map((genre) => (
                <span
                  key={genre}
                  className="rounded-full border border-primary/40 bg-primary/15 px-3 py-1 text-xs text-primary-foreground/90"
                >
                  {genre}
                </span>
              ))}
            </div>
          )}

          {item.overview && (
            <p className="mt-4 line-clamp-3 max-w-xl text-sm leading-relaxed text-foreground/90 sm:text-base">
              {item.overview}
            </p>
          )}

          <Button variant="white" size="lg" className="mt-6 font-bold" onClick={() => onOpen(item)}>
            View details
          </Button>
        </div>
      </div>
    </section>
  )
}
