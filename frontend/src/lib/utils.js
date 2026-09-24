import { clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

// Merges conditional class names, with later Tailwind utilities winning over
// earlier conflicting ones (e.g. cn('p-2', 'p-4') -> 'p-4'). Same helper the
// MovieFlix components were written against.
export function cn(...inputs) {
  return twMerge(clsx(inputs))
}
