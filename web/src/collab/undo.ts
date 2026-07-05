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

// Set true ONLY while a REMOTE peer's Y update is being applied into the store (ydoc.ts). The autosave
// subscriber reads it to skip persisting a peer's edit — the originating peer persists it, so N editors
// no longer each PUT the whole doc on every edit (N-way write amplification). Kept separate from the
// broad ydoc `applying` flag, which also covers local undo/redo (those SHOULD still autosave). Lives
// here (not ydoc.ts) so graph.ts can import it without the store→ydoc→store cycle.
export const collabApply = { remote: false }
