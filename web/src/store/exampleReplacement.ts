import type { CanvasRole } from '../api/client'
import type { CanvasDoc } from '../types/graph'

export type ExampleCreationIntent = 'create-separate' | 'replace-pristine'

export interface ExampleReplacementSnapshot {
  doc: CanvasDoc
  canvasRole: CanvasRole | null
  currentDraftId: string | null
  serverVersion: number | null
}

// Replacing a blank Canvas is deliberately narrower than "an empty graph": requirements and
// parameters are durable user-authored execution content too. Unknown ownership/persistence fails
// closed so an example is created separately.
export function isPristineExampleReplacement(snapshot: ExampleReplacementSnapshot): boolean {
  const { doc } = snapshot
  return snapshot.canvasRole === 'owner'
    && snapshot.currentDraftId == null
    && snapshot.serverVersion != null
    && doc.name === 'untitled'
    && doc.nodes.length === 0
    && doc.edges.length === 0
    && (doc.requirements?.length ?? 0) === 0
    && (doc.parameters?.length ?? 0) === 0
}

// Object identity intentionally detects even an edit that is later reverted while run history is
// loading. A confirmed replacement applies only to the exact Canvas snapshot the user saw.
export function isSameExampleReplacementSnapshot(
  left: ExampleReplacementSnapshot,
  right: ExampleReplacementSnapshot,
): boolean {
  return left.doc === right.doc
    && left.canvasRole === right.canvasRole
    && left.currentDraftId === right.currentDraftId
    && left.serverVersion === right.serverVersion
}
