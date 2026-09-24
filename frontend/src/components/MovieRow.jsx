import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useRef } from 'react'
import { MovieCard } from './MovieCard'
import { Skeleton } from './ui/Skeleton'

// A horizontally scrolling shelf of posters, as on the MovieFlix home page.
//
// Scrolling is native overflow rather than a carousel library: it keeps
// trackpad, wheel and touch behaviour exactly as the OS intends, and the
// arrows just nudge scrollLeft. Less code, better feel, nothing to sync.
export function MovieRow({ title, icon: Icon, items, loading, onOpen, onHover, onHoverEnd, onSeeAll }) {
  const scrollerRef = useRef(null)

  const nudge = (direction) => {
    const el = scrollerRef.current
    if (!el) return
    // Page by most of the visible width, leaving a sliver of the previous
    // card visible so the user keeps their place.
    el.scrollBy({ left: direction * el.clientWidth * 0.85, behavior: 'smooth' })
  }

  if (!loading && !items.length) return null

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="flex items-center gap-2 text-lg font-bold sm:text-xl">
          {Icon && <Icon className="h-5 w-5 text-primary" />}
          {title}
        </h2>
        <div className="flex items-center gap-1">
          {onSeeAll && (
            <button
              type="button"
              onClick={onSeeAll}
              className="mr-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              See all
            </button>
          )}
          <button
            type="button"
            aria-label="Scroll left"
            onClick={() => nudge(-1)}
            className="hidden h-8 w-8 items-center justify-center rounded-full border border-border bg-card transition-colors hover:bg-accent sm:flex"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <button
            type="button"
            aria-label="Scroll right"
            onClick={() => nudge(1)}
            className="hidden h-8 w-8 items-center justify-center rounded-full border border-border bg-card transition-colors hover:bg-accent sm:flex"
          >
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div
        ref={scrollerRef}
        className="no-scrollbar flex snap-x snap-mandatory gap-3 overflow-x-auto pb-2 sm:gap-4"
      >
        {loading
          ? Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="w-[140px] shrink-0 space-y-2 sm:w-[170px]">
                <Skeleton className="aspect-[2/3] w-full rounded-lg" />
                <Skeleton className="h-4 w-3/4" />
              </div>
            ))
          : items.map((item) => (
              <MovieCard
                key={`${item.content_type}-${item.slug}`}
                item={item}
                onOpen={onOpen}
                onHover={onHover}
                onHoverEnd={onHoverEnd}
                className="w-[140px] shrink-0 snap-start sm:w-[170px]"
              />
            ))}
      </div>
    </section>
  )
}
