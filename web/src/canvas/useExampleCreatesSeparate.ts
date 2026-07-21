import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useStore } from '../store/graph'

// Default to a new Canvas. Replacing in place is allowed only after every piece of durable evidence
// is available: ownership, a version-fenced remote blank, no local draft, and no prior run.
export function useExampleCreatesSeparate(): boolean {
  const doc = useStore((state) => state.doc)
  const canvasRole = useStore((state) => state.canvasRole)
  const currentDraftId = useStore((state) => state.currentDraftId)
  const serverVersion = useStore((state) => state.serverVersion)
  const [replacesPristine, setReplacesPristine] = useState(false)

  useEffect(() => {
    let cancelled = false
    const candidate = canvasRole === 'owner' && currentDraftId == null && serverVersion != null
      && doc.name === 'untitled' && doc.nodes.length === 0 && doc.edges.length === 0
    setReplacesPristine(false)
    if (!candidate) return () => { cancelled = true }
    void api.listRuns(doc.id)
      .then((runs) => { if (!cancelled) setReplacesPristine(runs.length === 0) })
      .catch(() => { /* fail closed: the action remains a separate example */ })
    return () => { cancelled = true }
  }, [canvasRole, currentDraftId, serverVersion, doc.id, doc.name, doc.nodes.length, doc.edges.length])

  return !replacesPristine
}
