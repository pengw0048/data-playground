import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { WorkspaceResource, WorkspaceSourceStatus } from '../types/api'
import { useStore } from '../store/graph'

type LocationState =
  | { kind: 'hidden' }
  | { kind: 'unavailable' }
  | { kind: 'resolved'; resource: WorkspaceResource; ancestors: WorkspaceResource[] }

type Props = {
  onReturnDestination: (resourceId: string | null | undefined) => void
  onNavigate: (resourceId: string | null) => void
}

function locationUnavailable(resource: WorkspaceResource, source: WorkspaceSourceStatus): boolean {
  return resource.detached || source.completeness !== 'complete'
    || source.referenceState === 'offline' || source.referenceState === 'permission_lost'
    || source.referenceState === 'detached' || source.referenceState === 'provider_error'
}

function displayAncestors(ancestors: WorkspaceResource[]): WorkspaceResource[] {
  // The local root is represented by the generic Workspace entry. Provider paths retain every
  // server-resolved ancestor after it; do not synthesize names or infer a path from provider ids.
  const first = ancestors[0]
  return first?.kind === 'container' && first.source === 'local' && first.parentId == null
    ? ancestors.slice(1) : ancestors
}

/**
 * Resolves Canvas placement only through the existing Workspace identity API.  A returned
 * parent id is safe to route to even when a provider is unavailable; it remains opaque and the
 * Workspace surface owns retry/relink recovery.  No response may update a newer Canvas.
 */
export function CanvasWorkspaceLocation({ onReturnDestination, onNavigate }: Props) {
  const canvasId = useStore((state) => state.doc.id)
  const serverVersion = useStore((state) => state.serverVersion)
  const currentDraftId = useStore((state) => state.currentDraftId)
  const currentDraftBaseVersion = useStore((state) => state.localDrafts.find(
    (draft) => draft.draftId === state.currentDraftId,
  )?.baseVersion ?? null)
  const [state, setState] = useState<LocationState>({ kind: 'hidden' })

  useEffect(() => {
    let current = true
    setState({ kind: 'hidden' })
    onReturnDestination(undefined)
    // Before bootstrap, `doc` is a throwaway Canvas and has no server version. A local-only draft
    // likewise has no authoritative Workspace identity. A draft shadowing an existing server
    // Canvas keeps its server base and may still use that Canvas's placement.
    const authoritativeVersion = currentDraftId ? currentDraftBaseVersion : serverVersion
    if (authoritativeVersion == null) return () => { current = false }

    void api.workspaceResource(`canvas:${canvasId}`).then((resolved) => {
      if (!current || resolved.resource?.kind !== 'canvas') return
      // `parentId` is an API-issued stable Workspace reference, never a reconstructed path.
      onReturnDestination(resolved.resource.parentId ?? null)
      if (locationUnavailable(resolved.resource, resolved.source)) {
        setState({ kind: 'unavailable' })
        return
      }
      setState({
        kind: 'resolved', resource: resolved.resource, ancestors: resolved.ancestors,
      })
    }).catch(() => {
      if (!current) return
      // A local draft or unplaced Canvas has no canonical location. Keep the generic menu entry.
      onReturnDestination(undefined)
      setState({ kind: 'hidden' })
    })
    return () => { current = false }
  }, [canvasId, currentDraftBaseVersion, currentDraftId, onReturnDestination, serverVersion])

  if (state.kind === 'hidden') return null
  if (state.kind === 'unavailable') {
    return <p role="status" className="text-[11px] text-muted-foreground">
      Its Workspace location is unavailable.
    </p>
  }

  return (
    <nav aria-label="Canvas Workspace location" className="flex min-w-0 items-center gap-1 overflow-hidden whitespace-nowrap text-[11px] text-muted-foreground">
      <button type="button" onClick={() => onNavigate(null)} className="shrink-0 hover:text-foreground">Workspace</button>
      {displayAncestors(state.ancestors).map((ancestor) => <span key={ancestor.id} className="flex min-w-0 items-center gap-1">
        <span aria-hidden>/</span>
        <button type="button" onClick={() => onNavigate(ancestor.id)} className="truncate hover:text-foreground">{ancestor.name}</button>
      </span>)}
      <span aria-hidden>/</span>
      <span aria-current="page" className="truncate text-foreground">{state.resource.name}</span>
    </nav>
  )
}
