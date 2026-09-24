import { SignInButton, SignUpButton, Show, useUser, UserButton } from '@clerk/react'
import { Film, Heart, Menu, Search, ShieldCheck, Tv, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { CLERK_APPEARANCE } from '../lib/clerkAppearance'
import { cn } from '../lib/utils'

// Top navigation, ported from the MovieFlix navbar.
//
// One deliberate difference: MovieFlix hardcoded a list of TMDB genre ids.
// Here the genres come from `genres` - the ones actually present in the
// indexed source catalog - so the menu can never offer a category that
// turns out to be empty.
export function Navbar({ view, contentType, genres, query, onQueryChange, onNavigate }) {
  // isLoaded gates whether Clerk has finished restoring the session from its
  // own storage - isSignedIn/user read as false/null for a beat on every
  // refresh otherwise, even for an already-signed-in user. Only isAdmin
  // needs this: it's a real permission check, so it must not flash "admin"
  // on for a signed-out visitor nor flash off for an admin mid-hydration.
  // Favorites has no such check - it's always shown (see below) - so it has
  // nothing to gate on and never needs to disappear/reappear on refresh.
  const { user, isSignedIn, isLoaded } = useUser()
  const isAdmin = isLoaded && isSignedIn && user?.publicMetadata?.role === 'admin'
  const [menuOpen, setMenuOpen] = useState(false)
  const [genresOpen, setGenresOpen] = useState(false)
  const genreRef = useRef(null)

  // Close the genre dropdown on an outside click - without this it stays
  // open over the content the user was trying to reach.
  useEffect(() => {
    if (!genresOpen) return
    const onDown = (e) => {
      if (genreRef.current && !genreRef.current.contains(e.target)) setGenresOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [genresOpen])

  const go = (target) => {
    setMenuOpen(false)
    setGenresOpen(false)
    onNavigate(target)
  }

  const navLink = (active) =>
    cn(
      'text-sm font-medium transition-colors',
      active ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
    )

  return (
    <header className="sticky top-0 z-50 border-b border-border bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/75">
      <div className="mx-auto flex h-16 max-w-[1400px] items-center gap-3 px-4">
        <button
          type="button"
          onClick={() => go({ view: 'home' })}
          className="flex shrink-0 items-center gap-2 font-bold"
        >
          <Film className="h-6 w-6 text-primary" />
          <span className="hidden text-lg sm:inline">MovieFlix</span>
        </button>

        <nav className="ml-4 hidden items-center gap-5 md:flex">
          <button
            type="button"
            className={navLink(view === 'home')}
            onClick={() => go({ view: 'home' })}
          >
            Home
          </button>
          <button
            type="button"
            className={navLink(view === 'browse' && contentType === 'movie')}
            onClick={() => go({ view: 'browse', contentType: 'movie' })}
          >
            Movies
          </button>
          <button
            type="button"
            className={cn(navLink(view === 'browse' && contentType === 'tv'), 'flex items-center gap-1')}
            onClick={() => go({ view: 'browse', contentType: 'tv' })}
          >
            <Tv className="h-4 w-4" />
            TV Shows
          </button>

          <div className="relative" ref={genreRef}>
            <button
              type="button"
              className={navLink(Boolean(view === 'browse' && genresOpen))}
              onClick={() => setGenresOpen((o) => !o)}
            >
              Genres
            </button>
            {genresOpen && (
              <div className="absolute left-0 top-full mt-2 max-h-[70vh] w-64 overflow-y-auto rounded-md border border-border bg-popover p-2 shadow-xl">
                {genres.length === 0 && (
                  <p className="px-2 py-3 text-sm text-muted-foreground">Building catalog…</p>
                )}
                {genres.map((genre) => (
                  <button
                    key={genre.name}
                    type="button"
                    onClick={() => go({ view: 'browse', contentType, genre: genre.name })}
                    className="flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-sm transition-colors hover:bg-accent"
                  >
                    <span>{genre.name}</span>
                    <span className="text-xs text-muted-foreground">{genre.count}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          <button
            type="button"
            className={cn(navLink(view === 'favorites'), 'flex items-center gap-1')}
            onClick={() => go({ view: 'favorites' })}
          >
            <Heart className="h-4 w-4" />
            Favorites
          </button>

          {isAdmin && (
            <button
              type="button"
              className={cn(navLink(view === 'admin'), 'flex items-center gap-1')}
              onClick={() => go({ view: 'admin' })}
            >
              <ShieldCheck className="h-4 w-4" />
              Admin
            </button>
          )}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={query}
              onChange={(e) => onQueryChange(e.target.value)}
              placeholder="Search titles..."
              className="h-9 w-36 rounded-md border border-border bg-card pl-8 pr-3 text-sm outline-none transition-[width,box-shadow] placeholder:text-muted-foreground focus:w-52 focus:ring-2 focus:ring-ring sm:w-52 sm:focus:w-72"
            />
          </div>

          <Show when="signed-out">
            <SignInButton mode="modal" appearance={CLERK_APPEARANCE}>
              <button
                type="button"
                className="h-9 rounded-md px-3 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
              >
                Sign in
              </button>
            </SignInButton>
            <SignUpButton mode="modal" appearance={CLERK_APPEARANCE}>
              <button
                type="button"
                className="h-9 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground transition-colors hover:opacity-90"
              >
                Sign up
              </button>
            </SignUpButton>
          </Show>
          <Show when="signed-in">
            <UserButton afterSignOutUrl="/" appearance={CLERK_APPEARANCE} />
          </Show>

          <button
            type="button"
            aria-label="Menu"
            className="flex h-9 w-9 items-center justify-center rounded-md border border-border md:hidden"
            onClick={() => setMenuOpen((o) => !o)}
          >
            {menuOpen ? <X className="h-4 w-4" /> : <Menu className="h-4 w-4" />}
          </button>
        </div>
      </div>

      {menuOpen && (
        <div className="border-t border-border bg-background px-4 py-3 md:hidden">
          <div className="flex flex-col gap-1">
            <button type="button" className="py-2 text-left text-sm" onClick={() => go({ view: 'home' })}>
              Home
            </button>
            <button
              type="button"
              className="py-2 text-left text-sm"
              onClick={() => go({ view: 'browse', contentType: 'movie' })}
            >
              Movies
            </button>
            <button
              type="button"
              className="py-2 text-left text-sm"
              onClick={() => go({ view: 'browse', contentType: 'tv' })}
            >
              TV Shows
            </button>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {genres.slice(0, 12).map((genre) => (
                <button
                  key={genre.name}
                  type="button"
                  onClick={() => go({ view: 'browse', contentType, genre: genre.name })}
                  className="rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                >
                  {genre.name}
                </button>
              ))}
            </div>
            <button
              type="button"
              className="mt-2 flex items-center gap-1 py-2 text-left text-sm"
              onClick={() => go({ view: 'favorites' })}
            >
              <Heart className="h-4 w-4" />
              Favorites
            </button>
            {isAdmin && (
              <button
                type="button"
                className="flex items-center gap-1 py-2 text-left text-sm"
                onClick={() => go({ view: 'admin' })}
              >
                <ShieldCheck className="h-4 w-4" />
                Admin
              </button>
            )}
          </div>
        </div>
      )}
    </header>
  )
}
