import { describe, it, expect } from 'vitest'
import { toGraph } from './client'
import type { CanvasDoc } from '../types/graph'

describe('toGraph wire serialization', () => {
  const doc: CanvasDoc = {
    id: 'c', version: 1, name: 't', requirements: [],
    nodes: [
      { id: 'a', type: 'source', position: { x: 0, y: 0 }, data: { title: 'src', config: { uri: 'events' }, status: 'latest' } },
      { id: 'j', type: 'join', position: { x: 1, y: 1 }, data: { title: 'j', config: {}, status: 'draft' } },
      { id: 'n', type: 'note', position: { x: 2, y: 2 }, data: { title: 'note', config: {} } },
    ],
    edges: [{ id: 'e', source: 'a', target: 'j', sourceHandle: null, targetHandle: null, data: { wire: 'dataset' } }],
  }

  it('carries per-node status on the wire so the server size estimator can trust a latest node’s actuals', () => {
    // regression: status was dropped, so routers/runs._actuals_for saw no 'latest' node and the
    // run-history-actuals estimate leg never fired in the app.
    const g = toGraph(doc)
    const byId = Object.fromEntries(g.nodes.map((n) => [n.id, n]))
    expect(byId['a'].data.status).toBe('latest')
    expect(byId['j'].data.status).toBe('draft')
  })

  it('drops note/code annotation nodes (no build step)', () => {
    expect(toGraph(doc).nodes.map((n) => n.id)).toEqual(['a', 'j'])
  })
})
