import { describe, expect, it } from 'vitest'
import { register, type NodeSpec } from '../nodes/registry'
import type { CanvasDoc, CanvasNode } from '../types/graph'
import { insertionEdge, insertionPorts } from './edgeInsertion'

function spec(kind: string, patch: Partial<NodeSpec> = {}): NodeSpec {
  const value: NodeSpec = {
    kind, title: kind, category: 'shape', canBypass: true, blurb: '',
    inputs: [{ id: 'in', wire: 'dataset' }], outputs: [{ id: 'out', wire: 'dataset' }],
    defaultData: () => ({ title: kind, status: 'draft', config: {} }), ...patch,
  }
  register(value, () => null)
  return value
}

function fixture(): CanvasDoc {
  spec('insertion-source', { inputs: [] })
  spec('insertion-target')
  const node = (id: string, type: string): CanvasNode => ({
    id, type, position: { x: 0, y: 0 }, data: { title: id, status: 'draft', config: {} },
  })
  return { id: 'canvas', version: 1, nodes: [node('s', 'insertion-source'), node('t', 'insertion-target')],
    edges: [{ id: 'edge', source: 's', sourceHandle: 'out', target: 't', targetHandle: 'in' }] }
}

describe('edge insertion compatibility', () => {
  it('checks both ends using live named-port types and accepts declarations', () => {
    const doc = fixture()
    spec('insertion-source', { inputs: [], outputs: [{ id: 'out', wire: 'sample' }] })
    const candidate = spec('linear', { inputs: [{ id: 'rows', wire: 'dataset', accepts: ['dataset', 'sample'] }] })
    doc.edges[0].data = { wire: 'metric' } // persisted decoration is not port authority
    expect(insertionPorts(doc, doc.edges[0], candidate)).toMatchObject({
      sourceWire: 'sample', input: { id: 'rows' }, output: { id: 'out', wire: 'dataset' },
    })
    spec('insertion-target', { inputs: [{ id: 'in', wire: 'metric', accepts: ['metric', 'dataset'] }] })
    expect(insertionPorts(doc, doc.edges[0], candidate)).not.toBeNull()
  })

  it.each([
    { inputs: [{ id: 'in', wire: 'metric' as const }] },
    { outputs: [{ id: 'out', wire: 'metric' as const }] },
    { inputs: [{ id: 'a', wire: 'dataset' as const }, { id: 'b', wire: 'dataset' as const }] },
    { outputs: [{ id: 'a', wire: 'dataset' as const }, { id: 'b', wire: 'dataset' as const }] },
    { outputs: [] },
  ])('rejects incompatible or ambiguous candidate ports: %j', (patch) => {
    const doc = fixture()
    expect(insertionPorts(doc, doc.edges[0], spec('candidate', patch))).toBeNull()
  })

  it('preserves explicit endpoints while refusing missing or ambiguous handles', () => {
    const doc = fixture()
    spec('insertion-source', { inputs: [], outputs: [{ id: 'out', wire: 'dataset' }, { id: 'other', wire: 'sample' }] })
    spec('insertion-target', { inputs: [{ id: 'in', wire: 'dataset' }, { id: 'other', wire: 'dataset' }] })
    const candidate = spec('linear')
    expect(insertionPorts(doc, doc.edges[0], candidate)).not.toBeNull()
    expect(insertionPorts(doc, { ...doc.edges[0], sourceHandle: null }, candidate)).toBeNull()
    expect(insertionPorts(doc, { ...doc.edges[0], targetHandle: null }, candidate)).toBeNull()
    expect(insertionPorts(doc, { ...doc.edges[0], sourceHandle: 'removed' }, candidate)).toBeNull()
  })

  it('uses effective outputs for a new Section rather than its static default port', () => {
    const doc = fixture()
    const section = spec('section', { defaultData: () => ({
      title: 'section', status: 'draft', config: { outputs: ['left', 'right'] },
    }) })
    expect(insertionPorts(doc, doc.edges[0], section)).toBeNull()
  })

  it('does not preserve a cycle or replace a wire on an already occupied single-input port', () => {
    const doc = fixture()
    const candidate = spec('linear')
    expect(insertionPorts({ ...doc, edges: [...doc.edges, { id: 'back', source: 't', target: 's' }] }, doc.edges[0], candidate)).toBeNull()
    const duplicate = { ...doc, edges: [...doc.edges, { ...doc.edges[0], id: 'second' }] }
    expect(insertionPorts(duplicate, doc.edges[0], candidate)).toBeNull()
    spec('insertion-target', { inputs: [{ id: 'in', wire: 'dataset', multi: true }] })
    expect(insertionPorts(duplicate, doc.edges[0], candidate)).not.toBeNull()
  })

  it('requires the same Canvas and original endpoints even if an edge id was reused', () => {
    const doc = fixture()
    const snapshot = { canvasId: doc.id, edge: { ...doc.edges[0] } }
    expect(insertionEdge(doc, snapshot)).toBe(doc.edges[0])
    expect(insertionEdge({ ...doc, id: 'other' }, snapshot)).toBeUndefined()
    expect(insertionEdge({ ...doc, edges: [] }, snapshot)).toBeUndefined()
    for (const patch of [{ source: 'other' }, { target: 'other' }, { sourceHandle: 'other' }, { targetHandle: 'other' }]) {
      expect(insertionEdge({ ...doc, edges: [{ ...doc.edges[0], ...patch }] }, snapshot)).toBeUndefined()
    }
  })
})
