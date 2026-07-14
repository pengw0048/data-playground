import { beforeEach, describe, expect, it } from 'vitest'
import { useStore } from '../store/graph'
import { hydrateFromRoomState, markYSyncReady, startYSync, stopYSync } from './ydoc'

const doc = {
  id: 'collab-test', version: 1, name: 'Collab test', edges: [], requirements: [],
  nodes: [{
    id: 'source', type: 'source', position: { x: 0, y: 0 },
    data: { title: 'Source', status: 'draft' as const, config: {}, history: [] },
  }],
}

describe('Yjs hydration decisions', () => {
  beforeEach(() => {
    stopYSync()
    useStore.setState({ doc })
  })

  it('seeds only after the relay says the room has no peers', () => {
    const sent: Uint8Array[] = []
    startYSync((update) => sent.push(update))

    hydrateFromRoomState(1)
    expect(sent).toEqual([])

    hydrateFromRoomState(0)
    expect(sent).toHaveLength(1)
    expect(sent[0].byteLength).toBeGreaterThan(0)
  })

  it('unblocks local edits after an empty peer sync reply', () => {
    const sent: Uint8Array[] = []
    startYSync((update) => sent.push(update))
    hydrateFromRoomState(1)  // a peer exists, so do not seed this client's snapshot

    markYSyncReady()  // the peer may legitimately hold an empty Y.Doc
    useStore.setState({ doc: { ...doc, name: 'Edited after empty sync' } })

    expect(sent).toHaveLength(1)
    expect(sent[0].byteLength).toBeGreaterThan(0)
  })
})
