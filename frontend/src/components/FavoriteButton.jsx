import { Heart } from 'lucide-react'
import { SignInButton } from '@clerk/react'
import { useFavorites } from '../hooks/useFavorites.jsx'
import { CLERK_APPEARANCE } from '../lib/clerkAppearance'
import { cn } from '../lib/utils'

// Heart toggle used on both the poster grid (small, overlaid) and the detail
// page (larger, inline next to Play). Signed-out users see the same heart
// but tapping it opens sign-in instead of silently failing - favoriting is
// meaningless without an account to attach it to.
export function FavoriteButton({ item, size = 'default', className }) {
  const { isLoaded, isFavorite, toggleFavorite, isSignedIn } = useFavorites()
  // Before Clerk finishes restoring the session (a beat on every hard
  // refresh), isSignedIn briefly reads false for an already-signed-in user -
  // rendering the heart as inactive/sign-in-prompting during that beat would
  // otherwise flash "not favorited" before flipping to the real state.
  const active = isLoaded && isSignedIn && isFavorite(item)

  const iconSize = size === 'sm' ? 'h-4 w-4' : 'h-5 w-5'
  const wrapperSize = size === 'sm' ? 'h-8 w-8' : 'h-11 w-11'

  const button = (
    <button
      type="button"
      aria-label={active ? 'Remove from favorites' : 'Add to favorites'}
      aria-pressed={active}
      onClick={(e) => {
        e.stopPropagation()
        if (isLoaded && isSignedIn) toggleFavorite(item)
      }}
      className={cn(
        'flex shrink-0 items-center justify-center rounded-full border transition-colors',
        active
          ? 'border-primary/60 bg-primary/20 text-primary'
          : 'border-white/20 bg-black/50 text-white hover:border-white/40 hover:bg-black/70',
        wrapperSize,
        className,
      )}
    >
      <Heart className={cn(iconSize, active && 'fill-current')} />
    </button>
  )

  // While Clerk is still hydrating, render the plain (non-sign-in-wrapped)
  // button - clicking during that brief window is a no-op above rather than
  // popping a sign-in modal for someone who is, in fact, already signed in.
  if (!isLoaded || isSignedIn) return button

  return (
    <SignInButton mode="modal" appearance={CLERK_APPEARANCE}>
      {button}
    </SignInButton>
  )
}
