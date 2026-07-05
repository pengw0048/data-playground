// Indirection so the Zustand store can delegate undo/redo to the CRDT UndoManager while a canvas is
// being co-edited — without a circular import (ydoc.ts already imports the store). ydoc.ts populates
// these while a sync is active and clears them when it stops. Offline, they stay null and the store
// falls back to its own full-doc snapshot stack.
type Fn = () => void

export const crdtUndo: { undo: Fn | null; redo: Fn | null; boundary: Fn | null } = {
  undo: null,
  redo: null,
  boundary: null,
}

/** True while a CRDT UndoManager is driving undo/redo (i.e. the canvas is being co-edited). */
export function crdtUndoActive(): boolean {
  return crdtUndo.undo !== null
}
