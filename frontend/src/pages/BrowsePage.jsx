import { useEffect, useState } from 'react'
import { fetchBrowse } from '../api'
import { MovieGrid } from '../components/MovieGrid'
import { Button } from '../components/ui/Button'
import { cn } from '../lib/utils'

const SORTS = [
  { id: 'popular', label: 'Popular' },
  { id: 'top_rated', label: 'Top Rated' },
  { id: 'newest', label: 'Newest' },
]

const PAGE_SIZE = 30

// Category / browse page: the indexed source catalog, filtered by genre and
// ordered by the chosen sort.
//
// Filtering happens server-side over the index (see /api/browse) rather than
// over whatever is already on screen, so "Drama" means every indexed drama,
// not just the dramas among the first 30 tiles.
export function BrowsePage({ contentType, genre, sort, genres, onNavigate, onOpen, onHover, onHoverEnd, indexStatus }) {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)

  // Reset to the first page whenever the listing itself changes; without
  // this, switching genre would keep appending to the previous list.
  useEffect(() => {
    setPage(1)
    setItems([])
  }, [contentType, genre, sort])

  useEffect(() => {
    let cancelled = false
    setLoading(true)

    fetchBrowse({ contentType, genre, sort, page, pageSize: PAGE_SIZE })
      .then((data) => {
        if (cancelled) return
        setTotal(data.total || 0)
        setHasMore(Boolean(data.has_more))
        // Page 1 replaces; later pages append.
        setItems((prev) => (page === 1 ? data.items || [] : [...prev, ...(data.items || [])]))
      })
      .catch(() => {
        if (!cancelled) setItems((prev) => (page === 1 ? [] : prev))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [contentType, genre, sort, page, indexStatus?.total])

  const heading = genre || (contentType === 'tv' ? 'TV Shows' : 'Movies')

  return (
    <div className="mx-auto max-w-[1400px] space-y-6 px-4 py-8">
      <div>
        <h1 className="text-2xl font-bold sm:text-3xl">{heading}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {total} {total === 1 ? 'title' : 'titles'} available from the source
          {indexStatus?.status === 'building' && ' · still indexing…'}
        </p>
      </div>

      {/* Movies / TV toggle */}
      <div className="flex flex-wrap items-center gap-2">
        {['movie', 'tv'].map((type) => (
          <Button
            key={type}
            size="sm"
            variant={contentType === type ? 'default' : 'outline'}
            onClick={() => onNavigate({ view: 'browse', contentType: type, genre: '', sort })}
          >
            {type === 'tv' ? 'TV Shows' : 'Movies'}
          </Button>
        ))}

        <span className="mx-1 hidden h-5 w-px bg-border sm:block" />

        {SORTS.map((option) => (
          <Button
            key={option.id}
            size="sm"
            variant={sort === option.id ? 'secondary' : 'ghost'}
            onClick={() => onNavigate({ view: 'browse', contentType, genre, sort: option.id })}
          >
            {option.label}
          </Button>
        ))}
      </div>

      {/* Genre chips - only genres present in the indexed catalog appear */}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => onNavigate({ view: 'browse', contentType, genre: '', sort })}
          className={cn(
            'rounded-full border px-3 py-1 text-xs transition-colors',
            !genre
              ? 'border-primary bg-primary text-primary-foreground'
              : 'border-border text-muted-foreground hover:bg-accent hover:text-foreground',
          )}
        >
          All
        </button>
        {genres.map((entry) => (
          <button
            key={entry.name}
            type="button"
            onClick={() => onNavigate({ view: 'browse', contentType, genre: entry.name, sort })}
            className={cn(
              'rounded-full border px-3 py-1 text-xs transition-colors',
              genre === entry.name
                ? 'border-primary bg-primary text-primary-foreground'
                : 'border-border text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
          >
            {entry.name}
            <span className="ml-1.5 opacity-60">{entry.count}</span>
          </button>
        ))}
      </div>

      <MovieGrid
        items={items}
        loading={loading && page === 1}
        skeletonCount={PAGE_SIZE}
        onOpen={onOpen}
        onHover={onHover}
        onHoverEnd={onHoverEnd}
        emptyMessage={
          indexStatus?.status === 'building'
            ? 'Still indexing the source catalog — check back in a moment.'
            : 'No titles here yet.'
        }
      />

      {hasMore && (
        <div className="flex justify-center pt-2">
          <Button variant="outline" disabled={loading} onClick={() => setPage((p) => p + 1)}>
            {loading ? 'Loading…' : 'Load more'}
          </Button>
        </div>
      )}
    </div>
  )
}
