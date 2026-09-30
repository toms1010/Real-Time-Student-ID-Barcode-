/**
 * Data-fetching hooks.
 *
 * Deliberately tiny: no react-query dependency.  Each hook exposes the four
 * things a screen actually needs - `data`, `loading`, `error`, `reload` - plus
 * a periodic refresh where the screen must stay live.  That is the whole
 * requirement, and it avoids a dependency whose upgrade cycle this project
 * would have to track.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api'

interface AsyncState<T> {
  data: T | null
  loading: boolean
  error: string | null
  errorCode: string | null
}

/**
 * Fetch `loader()` on mount and whenever `deps` change.
 * `intervalMs` re-fetches on a timer (pass 0 to disable) and pauses while the
 * document is hidden, so a background tab does not hammer the service.
 */
export function useApi<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  intervalMs = 0,
): AsyncState<T> & { reload: () => Promise<void>; setData: (value: T | null) => void } {
  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    loading: true,
    error: null,
    errorCode: null,
  })
  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const reload = useCallback(async () => {
    try {
      const value = await loaderRef.current()
      if (!mounted.current) return
      setState({ data: value, loading: false, error: null, errorCode: null })
    } catch (error) {
      if (!mounted.current) return
      const apiError = error instanceof ApiError ? error : null
      setState((previous) => ({
        data: previous.data,
        loading: false,
        error:
          error instanceof Error
            ? error.message
            : 'Something went wrong while loading the data.',
        errorCode: apiError?.code ?? null,
      }))
    }
  }, [])

  useEffect(() => {
    setState((previous) => ({ ...previous, loading: true }))
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    if (intervalMs <= 0) return
    let timer: number | undefined
    const tick = () => {
      if (document.visibilityState === 'visible') void reload()
      timer = window.setTimeout(tick, intervalMs)
    }
    timer = window.setTimeout(tick, intervalMs)
    return () => {
      if (timer) window.clearTimeout(timer)
    }
  }, [intervalMs, reload])

  const setData = useCallback((value: T | null) => {
    setState((previous) => ({ ...previous, data: value }))
  }, [])

  return { ...state, reload, setData }
}

/** Track a value that changes over time, for the "x seconds ago" labels. */
export function useTicker(intervalMs = 1000): number {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return tick
}

/** Local storage backed state, used for remembering filters and the theme. */
export function useLocalState<T>(key: string, initial: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(key)
      return stored ? (JSON.parse(stored) as T) : initial
    } catch {
      return initial
    }
  })
  const update = useCallback(
    (next: T) => {
      setValue(next)
      try {
        localStorage.setItem(key, JSON.stringify(next))
      } catch {
        /* private mode: the filter just will not be remembered */
      }
    },
    [key],
  )
  return [value, update]
}

/** Debounce a rapidly changing value (search boxes). */
export function useDebounced<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(timer)
  }, [value, delayMs])
  return debounced
}
