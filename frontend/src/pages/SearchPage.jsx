import { useEffect, useState } from 'react'
import { fetchCatalog } from '../api'
import { MovieGrid } from '../components/MovieGrid'
import { Button } from '../components/ui/Button'

// Search results.
//
// Deliberately hits the SOURCE's own search endpoint rather than filtering
// the index: the index only covers the pages crawled so far, while the
// source can search its whole library. Results are therefore real and
// playable, including titles the crawl has not reached yet.
//
// Catalog rows carry only title/poster/slug, so `rating`/`year` are absent
// here - the cards render without them rather than showing invented values.
export function SearchPage({ query, contentType, onNavigate, onOpen, onHover, onHoverEnd }) {
  const [items, setItems] = useState([])
  const [page, setPage] = useState(1)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setPage(1)
    setItems([])
  }, [query, contentType])

  useEffect(() => {
    if (!query.trim()) return
    let cancelled = false
    setLoading(true)
    setError(null)

    fetchCatalog('vidbox', contentType, query, page)
      .then((data) => {
        if (cancelled) return
        const batch = (data.movies || []).map((m) => ({
          slug: m.detail_page_slug,
          title: m.title,
          poster_url: m.thumbnail_url,
          content_type: m.content_type,
          rating: null,
          year: '',
          genres: [],
        }))
        setHasMore(batch.length >= 20)
        setItems((prev) => (page === 1 ? batch : [...prev, ...batch]))
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [query, contentType, page])

  return (
    <div className="mx-auto max-w-[1400px] space-y-6 px-4 py-8">
      <div>
        <h1 className="text-2xl font-bold sm:text-3xl">
          Results for <span className="text-primary">{query}</span>
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">Searching the source directly</p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {['movie', 'tv'].map((type) => (
          <Button
            key={type}
            size="sm"
            variant={contentType === type ? 'default' : 'outline'}
            onClick={() => onNavigate({ view: 'search', contentType: type })}
          >
            {type === 'tv' ? 'TV Shows' : 'Movies'}
          </Button>
        ))}
      </div>

      {error && (
        <p className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm">
          Search failed: {error}
        </p>
      )}

      <MovieGrid
        items={items}
        loading={loading && page === 1}
        skeletonCount={18}
        onOpen={onOpen}
        onHover={onHover}
        onHoverEnd={onHoverEnd}
        emptyMessage={`No ${contentType === 'tv' ? 'shows' : 'movies'} match "${query}".`}
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
