import { useEffect, useState } from 'react'
import { api } from '../api/client'
import {
  isPristineExampleReplacement,
  isSameExampleReplacementSnapshot,
  type ExampleCreationIntent,
  type ExampleReplacementSnapshot,
} from '../store/exampleReplacement'
import { useStore } from '../store/graph'

// The returned intent is the action described by the UI and is passed unchanged to the mutation.
// Pending, failed, or stale run-history evidence always describes (and performs) a separate create.
export function useExampleCreationIntent(enabled = true): ExampleCreationIntent {
  const doc = useStore((state) => state.doc)
  const canvasRole = useStore((state) => state.canvasRole)
  const currentDraftId = useStore((state) => state.currentDraftId)
  const serverVersion = useStore((state) => state.serverVersion)
  const [confirmedSnapshot, setConfirmedSnapshot] = useState<ExampleReplacementSnapshot | null>(null)
  const snapshot = { doc, canvasRole, currentDraftId, serverVersion }

  useEffect(() => {
    let cancelled = false
    const candidate = { doc, canvasRole, currentDraftId, serverVersion }
    setConfirmedSnapshot(null)
    if (!enabled || !isPristineExampleReplacement(candidate)) return () => { cancelled = true }
    void api.listRuns(doc.id)
      .then((runs) => {
        const latest = useStore.getState()
        const latestSnapshot = {
          doc: latest.doc,
          canvasRole: latest.canvasRole,
          currentDraftId: latest.currentDraftId,
          serverVersion: latest.serverVersion,
        }
        if (!cancelled && runs.length === 0
            && isPristineExampleReplacement(latestSnapshot)
            && isSameExampleReplacementSnapshot(candidate, latestSnapshot)) {
          setConfirmedSnapshot(candidate)
        }
      })
      .catch(() => { /* fail closed: the action remains a separate example */ })
    return () => { cancelled = true }
  }, [enabled, canvasRole, currentDraftId, serverVersion, doc])

  return enabled && confirmedSnapshot
    && isPristineExampleReplacement(snapshot)
    && isSameExampleReplacementSnapshot(confirmedSnapshot, snapshot)
    ? 'replace-pristine'
    : 'create-separate'
}
