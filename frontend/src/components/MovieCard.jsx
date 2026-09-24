import { Star } from 'lucide-react'
import { proxiedImageUrl } from '../api'
import { FavoriteButton } from './FavoriteButton'
import { useFavorites } from '../hooks/useFavorites.jsx'
import { cn } from '../lib/utils'

// Poster tile, ported from the MovieFlix card: 2:3 poster, hover zoom with a
// title overlay, and a title/year/rating footer.
//
// Images go through /api/proxy_image rather than being hotlinked directly -
// the image CDN rate-limits refererless bursts, which a grid of 20 posters
// would otherwise trigger.
export function MovieCard({ item, onOpen, onHover, onHoverEnd, className }) {
  const { slug, title, poster_url: posterUrl, rating, year, content_type: contentType } = item
  const { isFavorite } = useFavorites()
  const favorited = isFavorite(item)

  return (
    // A <div role="button"> rather than a real <button>: FavoriteButton
    // below renders its own <button>, and a <button> nested inside another
    // <button> is invalid HTML - browsers auto-close the outer one at that
    // point, which broke click handling (the heart's click bubbled up as a
    // card click too) and logged a hydration nesting warning.
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen?.(item)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen?.(item)
        }
      }}
      onMouseEnter={() => onHover?.(item)}
      onMouseLeave={() => onHoverEnd?.(item)}
      className={cn(
        'group h-full cursor-pointer overflow-hidden rounded-lg border border-border bg-card text-left',
        'transition-all duration-300 hover:scale-105 hover:shadow-lg hover:shadow-black/40',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        className,
      )}
    >
      <div className="relative aspect-[2/3] overflow-hidden bg-muted">
        {posterUrl ? (
          <img
            src={proxiedImageUrl(posterUrl)}
            alt={title}
            loading="lazy"
            className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-110"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center px-2 text-center text-xs text-muted-foreground">
            {title}
          </div>
        )}

        <div className="absolute inset-0 flex items-end bg-gradient-to-t from-black/80 via-transparent to-transparent p-3 opacity-0 transition-opacity duration-300 group-hover:opacity-100">
          <p className="line-clamp-3 text-xs font-medium text-white sm:text-sm">{title}</p>
        </div>

        {contentType === 'tv' && (
          <span className="absolute right-2 top-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white">
            TV
          </span>
        )}

        <FavoriteButton
          item={{ slug, title, poster_url: posterUrl, year, rating, content_type: contentType, source: item.source }}
          size="sm"
          className={cn(
            'absolute left-2 top-2 transition-opacity',
            favorited ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
          )}
        />

        {/* Continue Watching progress bar - only present on a history-sourced
            item (see useWatchHistory.jsx / HomePage's Continue Watching row),
            never on a plain catalog card. */}
        {item.progress_pct > 0 && (
          <div className="absolute inset-x-0 bottom-0 h-1 bg-black/60">
            <div className="h-full bg-primary" style={{ width: `${item.progress_pct}%` }} />
          </div>
        )}
      </div>

      <div className="p-2 sm:p-3">
        <h3 className="line-clamp-1 text-sm font-semibold sm:text-base">{title}</h3>
        <div className="mt-1 flex items-center justify-between text-xs text-muted-foreground sm:text-sm">
          <span>{year || '—'}</span>
          {rating != null && (
            <span className="flex items-center gap-1">
              <Star className="h-3 w-3 fill-yellow-400 text-yellow-400 sm:h-4 sm:w-4" />
              {rating.toFixed(1)}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
