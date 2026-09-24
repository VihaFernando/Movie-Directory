import { ClerkProvider } from '@clerk/react';
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import { CLERK_APPEARANCE } from './lib/clerkAppearance'
import { FavoritesProvider } from './hooks/useFavorites.jsx'
import { WatchHistoryProvider } from './hooks/useWatchHistory.jsx'

const PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ClerkProvider publishableKey={PUBLISHABLE_KEY} afterSignOutUrl="/" appearance={CLERK_APPEARANCE}>
      <FavoritesProvider>
        <WatchHistoryProvider>
          <App />
        </WatchHistoryProvider>
      </FavoritesProvider>
    </ClerkProvider>
  </StrictMode>,
)