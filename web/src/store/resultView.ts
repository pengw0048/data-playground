import { create } from 'zustand'

export type ResultView = {
  mode?: 'sample' | 'full'
  offset: number
  previousOffsets: number[]
}

// Presentation state only: rows, schemas, and artifact availability are always read afresh.
const MAX_RESULT_VIEWS = 20

export function resultViewKey(
  principalId: string | undefined, runId: string | undefined,
  nodeId: string | undefined, portId: string | undefined, uri: string | undefined,
): string | undefined {
  if (!principalId || !runId || !nodeId || !portId || !uri) return undefined
  return JSON.stringify([principalId, runId, nodeId, portId, uri])
}

export const useResultViewStore = create<{
  views: Record<string, ResultView>
  remember: (key: string | undefined, patch: Partial<ResultView>) => void
}>((set) => ({
  views: {},
  remember: (key, patch) => {
    if (!key) return
    set((state) => {
      const views = { ...state.views }
      const previous = views[key] ?? { offset: 0, previousOffsets: [] }
      delete views[key]
      views[key] = { ...previous, ...patch }
      const oldest = Object.keys(views)[0]
      if (Object.keys(views).length > MAX_RESULT_VIEWS) delete views[oldest]
      return { views }
    })
  },
}))
