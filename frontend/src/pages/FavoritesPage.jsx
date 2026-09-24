import { SignInButton } from '@clerk/react'
import { Heart } from 'lucide-react'
import { useEffect } from 'react'
import { MovieGrid } from '../components/MovieGrid'
import { useFavorites } from '../hooks/useFavorites.jsx'
import { CLERK_APPEARANCE } from '../lib/clerkAppearance'

// The signed-in user's saved movies and TV shows. Reads from the shared
// FavoritesContext (see hooks/useFavorites.jsx) rather than fetching here
// directly - that context already caches the list in sessionStorage per
// user, so revisiting this page within the same tab session is instant
// instead of re-hitting the server every time.
export function FavoritesPage({ onOpen, onHover, onHoverEnd }) {
  const { isLoaded, isSignedIn, favoritesList, favoritesListStatus, ensureListLoaded } = useFavorites()

  useEffect(() => {
    if (isLoaded && isSignedIn) ensureListLoaded()
  }, [isLoaded, isSignedIn, ensureListLoaded])

  // Clerk hasn't finished restoring the session yet (true on every hard
  // refresh for a beat) - render nothing definitive rather than the
  // signed-out prompt, which would otherwise flash before flipping to the
  // real, signed-in view.
  if (!isLoaded) {
    return <div className="mx-auto max-w-[1400px] px-4 py-8" />
  }

  if (!isSignedIn) {
    return (
      <div className="mx-auto flex max-w-[1400px] flex-col items-center gap-4 px-4 py-24 text-center">
        <Heart className="h-10 w-10 text-muted-foreground" />
        <h1 className="text-2xl font-bold">Sign in to see your favorites</h1>
        <p className="max-w-sm text-sm text-muted-foreground">
          Save movies and TV shows to come back to them later.
        </p>
        <SignInButton mode="modal" appearance={CLERK_APPEARANCE}>
          <button
            type="button"
            className="h-10 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:opacity-90"
          >
            Sign in
          </button>
        </SignInButton>
      </div>
    )
  }

  const loading = favoritesListStatus === 'loading' || favoritesListStatus === 'idle'

  return (
    <div className="mx-auto max-w-[1400px] space-y-6 px-4 py-8">
      <div>
        <h1 className="text-2xl font-bold sm:text-3xl">My Favorites</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {favoritesList.length} {favoritesList.length === 1 ? 'title' : 'titles'} saved
        </p>
      </div>

      <MovieGrid
        items={favoritesList}
        loading={loading}
        skeletonCount={12}
        onOpen={onOpen}
        onHover={onHover}
        onHoverEnd={onHoverEnd}
        emptyMessage="You haven't favorited anything yet. Tap the heart on a title to save it here."
      />
    </div>
  )
}
