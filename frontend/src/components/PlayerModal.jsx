import { X } from 'lucide-react'
import { useEffect } from 'react'
import { VideoPlayer } from './VideoPlayer'
import { proxiedImageUrl } from '../api'
import './PlayerModal.css'

// Fullscreen player overlay.
//
// The VideoPlayer inside keeps its own stylesheet: it is a working custom
// controls implementation (scrubbing, quality, subtitles, audio boost), and
// restyling its internals would risk breaking playback for no visual gain.
// Only this shell was ported to the new theme.
export function PlayerModal({ player }) {
  const { status, error, embed, close, posterUrl, season, episode } = player
  const isOpen = status !== 'idle'

  useEffect(() => {
    if (!isOpen) return
    const onKeyDown = (e) => {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isOpen, close])

  if (!isOpen) return null

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/85 p-4 sm:p-6"
      onClick={(e) => e.target === e.currentTarget && close()}
    >
      <div className="relative w-full max-w-6xl">
        <button
          type="button"
          onClick={close}
          title="Close"
          aria-label="Close player"
          className="absolute -top-11 right-0 flex h-9 w-9 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
        >
          <X className="h-5 w-5" />
        </button>

        <div className="relative aspect-video w-full overflow-hidden rounded-lg bg-black">
          {status === 'loading' && (
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
                {/* Naming the episode matters: resolving takes a while, and
                    the user needs to see the episode they picked is loading. */}
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

          {status === 'ready' && embed?.is_media && (
            <VideoPlayer player={player} posterUrl={posterUrl} />
          )}

          {status === 'ready' && !embed?.is_media && (
            <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-white/80">
              This source provides an HTML player with third-party ad and plugin code. It was blocked
              to keep playback local and ad-free.
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
