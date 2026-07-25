import { useEffect } from 'react'
import { api } from './api/client'
import { useStore } from './store/graph'

export const HUB_LIVENESS_INTERVAL_MS = 3_000
export const HUB_LIVENESS_TIMEOUT_MS = 1_500

function setReachable(reachable: boolean) {
  if (useStore.getState().kernelUp !== reachable) useStore.setState({ kernelUp: reachable })
}

// Reuse the hub's existing pure liveness endpoint. The interval plus request timeout bounds a
// just-missed failure to 4.5 seconds while keeping only one small request in flight at a time.
export function HubLiveness() {
  useEffect(() => {
    let active = true
    let generation = 0
    let controller: AbortController | null = null

    const check = () => {
      const requestGeneration = ++generation
      const requestController = new AbortController()
      controller?.abort()
      controller = requestController
      let timedOut = false
      const timeout = window.setTimeout(() => {
        timedOut = true
        requestController.abort()
        if (active && requestGeneration === generation) setReachable(false)
      }, HUB_LIVENESS_TIMEOUT_MS)

      void api.livez({ signal: requestController.signal }).then(({ ok }) => {
        if (active && !timedOut && requestGeneration === generation) setReachable(ok)
      }).catch(() => {
        if (active && !timedOut && requestGeneration === generation) setReachable(false)
      }).finally(() => {
        window.clearTimeout(timeout)
        if (controller === requestController) controller = null
      })
    }

    check()
    const interval = window.setInterval(check, HUB_LIVENESS_INTERVAL_MS)
    return () => {
      active = false
      generation += 1
      window.clearInterval(interval)
      controller?.abort()
    }
  }, [])

  return null
}
