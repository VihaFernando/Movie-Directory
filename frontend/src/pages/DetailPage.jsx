import { ArrowLeft, Clock, Play, Star } from 'lucide-react'
import { proxiedImageUrl } from '../api'
import { Button } from '../components/ui/Button'
import { FavoriteButton } from '../components/FavoriteButton'
import { MovieGrid } from '../components/MovieGrid'
import { Skeleton } from '../components/ui/Skeleton'
import { cn } from '../lib/utils'

// Formats TMDB's runtime minutes as "1h 42m" (or just "42m" under an hour) -
// matching how the reference MovieFlix design shows it.
function formatRuntime(minutes) {
  if (!minutes) return ''
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  return hours > 0 ? `${hours}h ${mins}m` : `${mins}m`
}

// Title page: a backdrop hero followed by cast, a trailer, an episode list
// (series only), and similar titles. Movies render the same hero/cast/
// trailer/similar sections and simply have no episode section.
//
// Metadata here comes from TMDB where available (fast), falling back to
// scraping the source; playback is always resolved by scraping, since TMDB
// has no idea where the video lives.
export function DetailPage({
  detail: detailState,
  contentType,
  onBack,
  onPlay,
  onPlayEpisode,
  currentEpisode,
  onOpenSimilar,
}) {
  const { status, error, detail, seasons, selectedSeason, selectSeason, episodes } = detailState
  const isSeries = contentType === 'tv'

  if (status === 'loading' || (!detail && status !== 'error')) {
    return (
      <div className="pb-16">
        <Skeleton className="h-[380px] w-full rounded-none sm:h-[480px]" />
        <div className="mx-auto max-w-[1400px] space-y-3 px-4 pt-8">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-4 w-full max-w-xl" />
          <Skeleton className="h-4 w-3/4 max-w-lg" />
        </div>
      </div>
    )
  }

  if (status === 'error' || !detail) {
    return (
      <div className="mx-auto max-w-[1400px] px-4 py-16 text-center">
        <p className="text-muted-foreground">Couldn't load this title: {error}</p>
        <Button variant="outline" className="mt-4" onClick={onBack}>
          Go back
        </Button>
      </div>
    )
  }

  return (
    <div className="pb-16">
      <section className="relative isolate flex min-h-[380px] items-end overflow-hidden sm:min-h-[480px]">
        {detail.backdrop_url && (
          <img
            src={proxiedImageUrl(detail.backdrop_url)}
            alt=""
            className="absolute inset-0 z-0 h-full w-full object-cover object-[center_18%]"
          />
        )}
        <div className="absolute inset-0 z-10 bg-gradient-to-t from-background via-background/60 to-background/10" />
        <div className="absolute inset-0 z-10 bg-gradient-to-r from-background/90 via-background/40 to-transparent" />

        <button
          type="button"
          onClick={onBack}
          aria-label="Back"
          className="absolute left-4 top-4 z-30 flex h-10 w-10 items-center justify-center rounded-full bg-black/50 transition-colors hover:bg-black/75"
        >
          <ArrowLeft className="h-5 w-5" />
        </button>

        <div className="relative z-20 mx-auto flex w-full max-w-[1400px] items-end gap-6 px-4 pb-10 pt-24">
          {detail.poster_url && (
            <img
              src={proxiedImageUrl(detail.poster_url)}
              alt={detail.title}
              className="hidden w-40 shrink-0 rounded-lg shadow-2xl sm:block lg:w-48"
            />
          )}

          <div className="min-w-0 max-w-2xl">
            <h1 className="text-3xl font-bold leading-tight sm:text-4xl lg:text-5xl">{detail.title}</h1>

            <div className="mt-3 flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
              <span className="uppercase">{isSeries ? 'TV' : 'Movie'}</span>
              {detail.rating != null && (
                <span className="flex items-center gap-1 font-semibold text-yellow-400">
                  <Star className="h-4 w-4 fill-yellow-400" />
                  {detail.rating.toFixed(1)}
                </span>
              )}
              {detail.year && <span>{detail.year}</span>}
              {detail.runtime > 0 && (
                <span className="flex items-center gap-1">
                  <Clock className="h-4 w-4" />
                  {formatRuntime(detail.runtime)}
                </span>
              )}
              {detail.status && detail.status !== 'Released' && <span>{detail.status}</span>}
            </div>

            {detail.genres?.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-2">
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

            {detail.overview && (
              <p className="mt-4 text-sm leading-relaxed text-foreground/90">{detail.overview}</p>
            )}

            <div className="mt-6 flex items-center gap-3">
              <Button variant="white" size="lg" className="font-bold" onClick={() => onPlay(detail)}>
                <Play className="h-4 w-4 fill-black" />
                Play
              </Button>
              <FavoriteButton
                item={{
                  slug: detail.slug,
                  title: detail.title,
                  poster_url: detail.poster_url,
                  year: detail.year,
                  rating: detail.rating,
                  content_type: contentType,
                  source: detail.source,
                }}
              />
            </div>
          </div>
        </div>
      </section>

      {detail.cast?.length > 0 && (
        <section className="mx-auto max-w-[1400px] px-4 pt-10">
          <h2 className="mb-4 text-xl font-bold sm:text-2xl">Cast</h2>
          <div className="grid grid-cols-3 gap-4 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8">
            {detail.cast.map((member) => (
              <div key={`${member.name}-${member.character}`} className="text-center">
                <div className="mb-2 aspect-[2/3] overflow-hidden rounded-lg bg-muted">
                  {member.profile_url ? (
                    <img
                      src={proxiedImageUrl(member.profile_url)}
                      alt={member.name}
                      loading="lazy"
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <div className="flex h-full w-full items-center justify-center text-xs text-muted-foreground">
                      {member.name}
                    </div>
                  )}
                </div>
                <p className="line-clamp-1 text-sm font-medium">{member.name}</p>
                {member.character && (
                  <p className="line-clamp-1 text-xs text-muted-foreground">{member.character}</p>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {detail.trailer_key && (
        <section className="mx-auto max-w-[1400px] px-4 pt-10">
          <h2 className="mb-4 text-xl font-bold sm:text-2xl">Trailer</h2>
          <div className="aspect-video overflow-hidden rounded-lg shadow-lg">
            <iframe
              src={`https://www.youtube.com/embed/${detail.trailer_key}`}
              title={`${detail.title} Trailer`}
              allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
              allowFullScreen
              className="h-full w-full"
            />
          </div>
        </section>
      )}

      {isSeries && (
        <section className="mx-auto max-w-[1400px] px-4 pt-10">
          <div className="mb-4 flex items-center justify-between gap-4">
            <h2 className="text-xl font-bold sm:text-2xl">Episodes</h2>
            {seasons.length > 0 && (
              <select
                value={selectedSeason ?? ''}
                onChange={(e) => selectSeason(Number(e.target.value))}
                aria-label="Season"
                className="h-10 rounded-md border border-border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-ring"
              >
                {seasons.map((season) => (
                  <option key={season.season_number} value={season.season_number}>
                    {season.name}
                  </option>
                ))}
              </select>
            )}
          </div>

          {status === 'refreshing' && (
            <p className="py-3 text-sm text-muted-foreground">Loading season…</p>
          )}

          {status === 'ready' && episodes.length === 0 && (
            <p className="py-3 text-sm text-muted-foreground">No episodes listed for this season.</p>
          )}

          <ul className="flex flex-col gap-3">
            {episodes.map((episode) => {
              const isCurrent =
                currentEpisode &&
                currentEpisode.season === episode.season_number &&
                currentEpisode.episode === episode.episode_number

              return (
                <li key={`${episode.season_number}-${episode.episode_number}`}>
                  <button
                    type="button"
                    onClick={() => onPlayEpisode(episode.season_number, episode.episode_number)}
                    className={cn(
                      'group flex w-full gap-4 rounded-lg border p-3 text-left transition-colors',
                      isCurrent
                        ? 'border-primary/60 bg-card'
                        : 'border-transparent bg-card/60 hover:border-border hover:bg-card',
                    )}
                  >
                    <div className="relative aspect-video w-32 shrink-0 overflow-hidden rounded-md bg-muted sm:w-44">
                      {episode.still_url && (
                        <img
                          src={proxiedImageUrl(episode.still_url)}
                          alt=""
                          loading="lazy"
                          className="h-full w-full object-cover"
                        />
                      )}
                      <span className="absolute left-0 top-0 rounded-br-md bg-black/75 px-2 py-0.5 text-xs font-bold">
                        {episode.episode_number}
                      </span>
                      <span className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100">
                        <Play className="h-7 w-7 fill-white text-white" />
                      </span>
                    </div>

                    <div className="min-w-0 flex-1 self-center">
                      <h3 className="text-sm font-semibold sm:text-base">{episode.title}</h3>
                      {episode.overview && (
                        <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground sm:text-sm">
                          {episode.overview}
                        </p>
                      )}
                    </div>
                  </button>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      {detail.similar?.length > 0 && (
        <section className="mx-auto max-w-[1400px] px-4 pt-10">
          <h2 className="mb-4 text-xl font-bold sm:text-2xl">More Like This</h2>
          <MovieGrid items={detail.similar} onOpen={onOpenSimilar} />
        </section>
      )}
    </div>
  )
}
