import { useEffect, useState } from 'react'

// Returns `value` only after it has stopped changing for `delay` ms.
//
// Search hits the source's own search endpoint, which is a real scrape on
// the backend - firing one per keystroke would queue a browser render for
// every letter typed. Debouncing means one request per pause in typing.
export function useDebounced(value, delay = 400) {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(id)
  }, [value, delay])

  return debounced
}
