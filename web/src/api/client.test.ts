import { afterEach, describe, it, expect, vi } from 'vitest'
import { api, setApiUser, toGraph } from './client'
import type { CanvasDoc } from '../types/graph'

afterEach(() => {
  setApiUser(null)
  vi.restoreAllMocks()
})

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

describe('run-scoped result access', () => {
  it('samples a persisted output by run/node/port identity instead of a client-provided URI', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      columns: [], rows: [], truncated: false, completeness: 'complete',
      notPreviewable: false, wire: 'dataset',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    await api.runOutputSample('run / 1', 'node-a', 'port-b', 50, 100)

    expect(fetchMock).toHaveBeenCalledWith('/api/run/run%20%2F%201/sample', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ nodeId: 'node-a', portId: 'port-b', k: 50, offset: 100 }),
    }))
  })

  it('uses the same open-mode identity hint for export preflight and iframe download', async () => {
    setApiUser('robot researcher')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }))

    const url = api.fullResultExportUrl('run-1', 'node-a', 'out', 'robot data')
    const preflightUrl = await api.preflightFullResultExport('run-1', 'node-a', 'out', 'robot data')

    expect(preflightUrl).toBe(url)
    const parsed = new URL(url, 'http://localhost')
    expect(parsed.pathname).toBe('/api/run/run-1/export')
    expect(parsed.searchParams.get('nodeId')).toBe('node-a')
    expect(parsed.searchParams.get('portId')).toBe('out')
    expect(parsed.searchParams.get('filename')).toBe('robot data')
    expect(parsed.searchParams.get('userId')).toBe('robot researcher')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/run/run-1/export?nodeId=node-a&portId=out&filename=robot+data&userId=robot+researcher',
      expect.objectContaining({
        method: 'HEAD', headers: expect.objectContaining({ 'X-DP-User': 'robot researcher' }),
      }),
    )
  })
})

describe('settings batch client', () => {
  it('sends the expected revision and dirty changes in one request', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      ok: true, revision: { global: 4, user: 7 },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    await api.putSettingsBatch(
      { global: 3, user: 7 },
      [{ scope: 'global', key: 'agentModel', value: 'openai/gpt-5' }],
    )

    expect(fetchMock).toHaveBeenCalledWith('/api/settings/batch', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({
        expectedRevision: { global: 3, user: 7 },
        changes: [{ scope: 'global', key: 'agentModel', value: 'openai/gpt-5' }],
      }),
    }))
  })
})
