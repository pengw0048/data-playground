import { describe, it, expect, beforeEach, vi } from 'vitest'

// the store module runs autosave side-effects at import; stub the network client so nothing escapes.
// (Autosave is gated on _bootstrapped=false at import, so no PUT fires here anyway.)
vi.mock('../api/client', () => ({ api: new Proxy({}, { get: () => async () => ({}) }) }))

import { useStore } from './graph'

const NODE = (id: string, type = 'source') => ({ id, type, position: { x: 0, y: 0 }, data: {} })

describe('graph store — core authority ops', () => {
  beforeEach(() => {
    // start each test from a known empty doc
    useStore.setState({ doc: { id: 'c', version: 1, name: 'test', nodes: [], edges: [], requirements: [] }, past: [], future: [] })
  })

  it('applyAgentGraph REPLACES nodes/edges and marks them stale (undoable)', () => {
    useStore.getState().applyAgentGraph({
      nodes: [NODE('a'), { id: 'b', type: 'filter', position: { x: 1, y: 1 }, data: { title: 'keep' } }],
      edges: [{ id: 'e', source: 'a', target: 'b', data: { wire: 'dataset' } }],
    })
    const doc = useStore.getState().doc
    expect(doc.nodes.map((n) => n.id)).toEqual(['a', 'b'])
    expect(doc.edges.map((e) => e.id)).toEqual(['e'])
    expect(doc.nodes.every((n) => n.data.status === 'stale')).toBe(true)  // touched → user can preview/run
    expect(useStore.getState().past.length).toBe(1)                        // pushed an undo snapshot

    // a SECOND apply replaces (does not append) — proves it's safe to import onto a fresh file only
    useStore.getState().applyAgentGraph({ nodes: [NODE('z')], edges: [] })
    expect(useStore.getState().doc.nodes.map((n) => n.id)).toEqual(['z'])
  })

  it('undo restores the pre-apply doc', () => {
    useStore.getState().applyAgentGraph({ nodes: [NODE('a')], edges: [] })
    expect(useStore.getState().doc.nodes).toHaveLength(1)
    useStore.getState().undo()
    expect(useStore.getState().doc.nodes).toHaveLength(0)  // back to the empty baseline
  })

  it('pushToast adds a toast and dismissToast removes it', () => {
    useStore.getState().pushToast('boom', 'error')
    const t = useStore.getState().toasts.find((x) => x.msg === 'boom')
    expect(t?.kind).toBe('error')
    useStore.getState().dismissToast(t!.id)
    expect(useStore.getState().toasts.some((x) => x.msg === 'boom')).toBe(false)
  })
})
