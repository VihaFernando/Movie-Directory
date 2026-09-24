import { cn } from '../../lib/utils'

// Trimmed port of the shadcn/ui button: only the variants and sizes this
// app actually uses, so there is no unused surface to keep in step.
const VARIANTS = {
  default: 'bg-primary text-primary-foreground hover:bg-primary/90',
  secondary: 'bg-secondary text-secondary-foreground hover:bg-secondary/80',
  outline: 'border border-border bg-transparent hover:bg-accent hover:text-accent-foreground',
  ghost: 'hover:bg-accent hover:text-accent-foreground',
  white: 'bg-white text-black hover:bg-white/90',
}

const SIZES = {
  sm: 'h-9 px-3 text-sm',
  default: 'h-10 px-4 py-2 text-sm',
  lg: 'h-11 px-8 text-base',
  icon: 'h-10 w-10',
}

export function Button({ className, variant = 'default', size = 'default', ...props }) {
  return (
    <button
      className={cn(
        'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-medium',
        'ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2',
        'focus-visible:ring-ring focus-visible:ring-offset-2',
        'disabled:pointer-events-none disabled:opacity-50',
        VARIANTS[variant] ?? VARIANTS.default,
        SIZES[size] ?? SIZES.default,
        className,
      )}
      {...props}
    />
  )
}
