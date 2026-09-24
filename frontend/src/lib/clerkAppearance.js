import { dark } from '@clerk/themes'

// baseTheme: dark ensures every internal Clerk element (labels, dividers,
// secondary text, icons, dropdown menus, and modals spawned from menus like
// UserProfile) is dark by default - variables alone only cover the elements
// Clerk maps to them, not the full component tree. Colors on top match a
// pure-black/violet look. Set globally on ClerkProvider so it reaches every
// Clerk component, including ones not rendered directly (e.g. UserProfile
// opened from UserButton's "Manage account").
export const CLERK_APPEARANCE = {
  baseTheme: dark,
  variables: {
    colorPrimary: '#7c3aed',
    colorBackground: '#000000',
    colorForeground: '#ffffff',
    colorText: '#ffffff',
    colorInput: '#0a0a0a',
    colorInputForeground: '#ffffff',
    colorTextSecondary: '#a1a1aa',
    colorNeutral: '#ffffff',
    colorDanger: '#ef4444',
    borderRadius: '0.5rem',
  },
  elements: {
    card: 'shadow-xl border border-white/10',
  },
}
