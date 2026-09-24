import { cn } from '../../lib/utils'

// Placeholder block shown while data loads. Matches MovieFlix's skeleton:
// a muted, pulsing rounded box sized by whatever classes the caller passes.
export function Skeleton({ className, ...props }) {
  return <div className={cn('animate-pulse rounded-md bg-muted', className)} {...props} />
}
