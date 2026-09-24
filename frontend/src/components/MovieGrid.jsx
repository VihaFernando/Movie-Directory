import { MovieCard } from './MovieCard'
import { Skeleton } from './ui/Skeleton'

// Responsive poster grid: 2 columns on phones up to 6 on wide screens,
// matching the MovieFlix breakpoints.
export function MovieGrid({ items, loading, skeletonCount = 12, onOpen, onHover, onHoverEnd, emptyMessage }) {
  if (loading) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 sm:gap-4 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
        {Array.from({ length: skeletonCount }).map((_, i) => (
          <div key={i} className="space-y-2">
            <Skeleton className="aspect-[2/3] w-full rounded-lg" />
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        ))}
      </div>
    )
  }

  if (!items.length) {
    return (
      <p className="py-12 text-center text-muted-foreground">
        {emptyMessage || 'Nothing to show here yet.'}
      </p>
    )
  }

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 sm:gap-4 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
      {items.map((item) => (
        <MovieCard
          key={`${item.content_type}-${item.slug}`}
          item={item}
          onOpen={onOpen}
          onHover={onHover}
          onHoverEnd={onHoverEnd}
        />
      ))}
    </div>
  )
}
