import { afterEach, describe, it, expect, vi } from 'vitest'
import {
  api, KernelError, managedDatasetNameErrorMessage, setApiUser, toGraph, toMergeColumnsGraph,
} from './client'
import type { CanvasDoc } from '../types/graph'
import type { WriteIntent } from '../types/api'

afterEach(() => {
  setApiUser(null)
  vi.restoreAllMocks()
})

describe('API error recovery contract', () => {
  it('keeps local Workspace sorting and filtering in the local source lens', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify({ container: null, items: [], hasMore: false, completeness: 'complete', sources: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))

    await api.workspaceBrowse('folder/one', {
      limit: 50, cursor: 'next page', source: 'local',
      sort: 'updated', order: 'desc', kinds: ['canvas', 'dataset'],
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/workspace/containers/folder%2Fone?cursor=next+page&limit=50&source=local&sort=updated&order=desc&kind=canvas&kind=dataset',
      expect.objectContaining({}),
    )
  })

  it('can create a Canvas beside the currently open Canvas', async () => {
    const doc: CanvasDoc = { id: 'new-canvas', name: 'New Canvas', version: 1, nodes: [], edges: [] }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify({ ok: true, id: doc.id, version: 1, created: true }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))

    await expect(api.createCanvas(doc, { besideCanvasId: 'current/canvas' }))
      .resolves.toMatchObject({ created: true })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/canvas?besideCanvasId=current%2Fcanvas',
      expect.objectContaining({ method: 'POST', body: JSON.stringify(doc) }),
    )
  })

  it('asks the server to resolve example Sources in one authoritative local batch', async () => {
    const payload = { resolutions: [{
      ref: 'events', state: 'resolved',
      table: { id: 'tbl-events', registrationId: 'reg-events', name: 'events', uri: '/data/events.parquet', columns: [] },
    }] }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify(payload),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))

    await expect(api.resolveExampleSources(['events'])).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/catalog/example-sources/resolve',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ refs: ['events'] }) }),
    )
  })

  it('marks the current owner’s visible Inbox items read with one batch request', async () => {
    const payload = { markedCount: 3, readAt: '2026-07-29T12:00:00Z' }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify(payload),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))

    await expect(api.inboxMarkAllRead()).resolves.toEqual(payload)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/inbox/read-all',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('asks the server to resolve a current retained result without accepting result identity', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      runId: 'retained-run',
      executionManifestSha256: 'a'.repeat(64),
      parameterBindings: [],
      output: {
        nodeId: 'transform', portId: 'out', wire: 'dataset',
        publicationKind: 'result', outcome: 'committed', uri: '/private/result.parquet', rows: 2,
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const doc: CanvasDoc = {
      id: 'canvas', version: 1, nodes: [{
        id: 'transform', type: 'transform', position: { x: 0, y: 0 },
        data: { title: 'Transform', status: 'latest', config: {
          source: 'adhoc', mode: 'map', code: 'def fn(row): return row',
        } },
      }], edges: [],
    }

    await api.retainedResult(doc, 'transform', 'out')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/run/retained-result',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          graph: toGraph(doc), nodeId: 'transform', portId: 'out',
        }),
      }),
    )
    const body = String((fetchMock.mock.calls[0]?.[1] as RequestInit).body)
    expect(body).not.toContain('runId')
    expect(body).not.toContain('uri')
  })

  it('asks the server to discover retained editor input without sending a run id or artifact URI', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      columns: [], rows: [], truncated: false,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const doc: CanvasDoc = {
      id: 'canvas', version: 1, nodes: [{
        id: 'transform', type: 'transform', position: { x: 0, y: 0 },
        data: { title: 'Transform', config: {
          source: 'adhoc', mode: 'map', code: 'def fn(row): return row',
        } },
      }], edges: [],
    }

    await api.retainedEditorPreview(doc, 'transform', 20, 5, 'out', [])

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/run/editor-preview',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          graph: toGraph(doc), nodeId: 'transform', portId: 'out',
          k: 20, offset: 5, parameterBindings: [],
        }),
      }),
    )
    const body = String((fetchMock.mock.calls[0]?.[1] as RequestInit).body)
    expect(body).not.toContain('runId')
    expect(body).not.toContain('uri')
  })

  it('sends Example rows only to the editor-local preview endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      columns: [], rows: [], truncated: false,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const doc: CanvasDoc = {
      id: 'canvas', version: 1, nodes: [{
        id: 'transform', type: 'transform', position: { x: 0, y: 0 },
        data: { title: 'Transform', config: {
          source: 'adhoc', mode: 'map', code: 'def fn(row): return row',
        } },
      }], edges: [],
    }
    const fixture = '[{"value":1}]'

    await api.exampleRowsEditorPreview(doc, 'transform', fixture, 20, 5, 'out', [])

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/run/editor-preview/examples',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          graph: toGraph(doc), nodeId: 'transform', exampleRowsJson: fixture,
          portId: 'out', k: 20, offset: 5, parameterBindings: [],
        }),
      }),
    )
    const body = String((fetchMock.mock.calls[0]?.[1] as RequestInit).body)
    expect(body).not.toContain('runId')
    expect(body).not.toContain('artifact')
  })

  it('preserves the stable machine code and retryability for revision recovery UI', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      detail: 'dataset_revision_provider_offline', code: 'service_unavailable', retryable: true,
    }), { status: 503, headers: { 'Content-Type': 'application/json' } }))

    const error = await api.datasetRevision('dataset-a', 'revision-1').catch((caught) => caught)
    expect(error).toBeInstanceOf(KernelError)
    expect(error).toMatchObject({
      status: 503, message: 'dataset_revision_provider_offline',
      code: 'service_unavailable', retryable: true,
    })
  })

  it('sends opaque exact revision IDs in the request body', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      detail: 'dataset_revision_unavailable', code: 'resource_gone', retryable: false,
    }), { status: 410, headers: { 'Content-Type': 'application/json' } }))

    await api.datasetRevision(
      'luma-data-api://table/1530',
      'luma-data-exact://table/1530/revision/2/identity/73cb',
    ).catch(() => undefined)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/catalog/revision-details',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({
        datasetId: 'luma-data-api://table/1530',
        revisionId: 'luma-data-exact://table/1530/revision/2/identity/73cb',
      }) }),
    )
  })

  it('preserves field-specific managed-name diagnostics without parsing detail', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      detail: 'localized prose may change',
      code: 'invalid_managed_dataset_name',
      retryable: false,
      field: 'filename',
      reason: 'path_syntax',
    }), { status: 422, headers: { 'Content-Type': 'application/json' } }))

    const doc: CanvasDoc = {
      id: 'managed-name-error',
      version: 1,
      nodes: [{
        id: 'write',
        type: 'write',
        position: { x: 0, y: 0 },
        data: { title: 'write', config: { filename: '../escape.parquet' } },
      }],
      edges: [],
    }
    const error = await api.writeAdmission(
      doc,
      'write',
      '11111111-1111-4111-8111-111111111111',
    ).catch((caught) => caught)
    expect(error).toBeInstanceOf(KernelError)
    expect(error).toMatchObject({
      status: 422,
      code: 'invalid_managed_dataset_name',
      field: 'filename',
      reason: 'path_syntax',
    })
    expect(managedDatasetNameErrorMessage(error)).toBe(
      'Use one managed dataset name, without a path or URI.')
  })
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

  it('sends only the direct Source → Select → Write chain to certified merge admission', () => {
    const mergeDoc: CanvasDoc = {
      ...doc,
      nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'latest', config: { uri: 'exact.parquet', datasetRef: { kind: 'exact', datasetId: 'd', revisionId: 'r' } } } },
        { id: 'select', type: 'select', position: { x: 1, y: 0 }, data: { title: 'select', status: 'draft', config: { select: 'id, score' } } },
        { id: 'write', type: 'write', position: { x: 2, y: 0 }, data: { title: 'write', status: 'draft', config: { filename: 'exact.parquet' } } },
        { id: 'unrelated', type: 'filter', position: { x: 9, y: 9 }, data: { title: 'unrelated', status: 'draft', config: { predicate: 'x > 0' } } },
      ],
      edges: [
        { id: 'source-select', source: 'source', target: 'select', data: { wire: 'dataset' } },
        { id: 'select-write', source: 'select', target: 'write', data: { wire: 'dataset' } },
        { id: 'unrelated-write', source: 'unrelated', target: 'write', data: { wire: 'dataset' } },
      ],
    }
    const graph = toMergeColumnsGraph(mergeDoc, 'write')
    // The extra direct input is retained as evidence for authoritative shape rejection; unrelated
    // parts of the canvas remain absent and can never join this certified request.
    expect(graph.nodes.map((node: any) => node.id).sort()).toEqual(['select', 'source', 'unrelated', 'write'])
    expect(graph.edges.map((edge: any) => edge.id).sort()).toEqual(['select-write', 'source-select', 'unrelated-write'])
    expect(graph.nodes.some((node: any) => node.id === 'a')).toBe(false)
  })

  it('sends an Agent request with only the current prompt and current graph payload', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      available: true, graph: { nodes: [], edges: [] }, summary: 'Done.', transcript: [],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    await api.agentAct(doc, 'build a current filter')

    expect(fetchMock).toHaveBeenCalledWith('/api/agent', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ outcome: 'build a current filter', graph: toGraph(doc) }),
    }))
  })
})

describe('run-scoped result access', () => {
  it('resubmits a frozen write with its admitted producer version after Canvas autosave', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      runId: 'run-1', status: 'running', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [], outputs: [],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const doc: CanvasDoc = {
      id: 'canvas', version: 8, nodes: [{
        id: 'write', type: 'write', position: { x: 0, y: 0 },
        data: { title: 'write', status: 'failed', config: { filename: 'output.parquet' } },
      }], edges: [],
    }
    const intent: WriteIntent = {
      destination: { logicalUri: '/outputs/output.parquet', name: 'output', provider: 'managed-local-file' },
      mode: 'create', expectedSchema: [], expectedHead: null, idempotencyKey: 'write-key', partitions: [],
      provenance: { publication: {
        idempotencyKey: 'write-key', producer: 'canvas', producerVersion: 7, provenance: 'run',
      }, parents: [] },
    }

    await api.run(doc, 'write', true, 'submission', undefined, intent, undefined, intent)

    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body))
    expect(body.graph.version).toBe(7)
    expect(body.writeIntent).toEqual(intent)
    expect(body.confirmedWriteIntent).toEqual(intent)
    expect(doc.version).toBe(8)
  })

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

describe('inspection input manifests', () => {
  it('carries the retained preview manifest through sampled and full profile requests', async () => {
    setApiUser('binding-user')
    const doc: CanvasDoc = {
      id: 'binding-canvas', version: 1, name: 'binding', requirements: [],
      nodes: [{
        id: 'source', type: 'source', position: { x: 0, y: 0 },
        data: { title: 'source', config: { uri: 'input.lance' }, status: 'draft' },
      }],
      edges: [],
    }
    const inputManifest = [{
      node_id: 'source', dataset_id: 'dataset', revision_id: '7', provider: 'lance',
      resolved_at: '2026-07-16T00:00:00Z',
    }]
    const parameterBindings = [{ name: 'threshold', value: 10 }]
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (path) => {
      if (path === '/api/run/preview') {
        return new Response(JSON.stringify({
          columns: [], rows: [], truncated: true, completeness: 'sample',
          rowLimit: 2000, limitReason: 'preview-scan', limitScope: 'each-source',
          notPreviewable: false, wire: 'dataset', inputManifest,
        }), { status: 200 })
      }
      if (path === '/api/run/profile-estimate') {
        return new Response(JSON.stringify({
          rows: 1, bytes: 8, placement: 'local', needsConfirm: false,
          targetPortId: 'out', planDigest: 'a'.repeat(64), inputManifest,
        }), { status: 200 })
      }
      if (path === '/api/run/profile-job') {
        return new Response(JSON.stringify({
          runId: 'profile-1', status: 'queued', jobType: 'profile', targetNodeId: 'source',
          targetPortId: 'out', rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
          outputs: [], planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
        }), { status: 200 })
      }
      return new Response(JSON.stringify({
        columns: [], rowCount: 0, sampled: true, completeness: 'sample',
        notPreviewable: false, inputManifest,
      }), { status: 200 })
    })

    await api.preview(doc, 'source', 50, 0, undefined, inputManifest, parameterBindings)
    await api.profile(doc, 'source', undefined, inputManifest, parameterBindings)
    await api.profileEstimate(doc, 'source', 'out', inputManifest, parameterBindings)
    await api.fullProfile(doc, 'source', 'out', 'a'.repeat(64), crypto.randomUUID(), true, inputManifest, parameterBindings)

    for (const path of ['/api/run/preview', '/api/run/profile', '/api/run/profile-estimate', '/api/run/profile-job']) {
      const call = fetchMock.mock.calls.find(([observed]) => observed === path)
      expect(JSON.parse(String(call?.[1]?.body)).inputManifest).toEqual(inputManifest)
      expect(JSON.parse(String(call?.[1]?.body)).parameterBindings).toEqual(parameterBindings)
    }
  })

  it('carries optional bindings through schema, graph estimate, plan, and join analysis', async () => {
    const doc: CanvasDoc = {
      id: 'parameter-canvas', version: 1, name: 'parameterized', requirements: [],
      parameters: [{ name: 'threshold', type: 'integer', required: true }],
      nodes: [{
        id: 'target', type: 'filter', position: { x: 0, y: 0 },
        data: { title: 'target', config: { threshold: { parameterRef: 'threshold' } }, status: 'draft' },
      }], edges: [],
    }
    const parameterBindings = [{ name: 'threshold', value: 10 }]
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({}), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))

    await api.schema(doc, 'target', undefined, parameterBindings)
    await api.graphSizes(doc, 'target', parameterBindings)
    await api.plan(doc, 'target', parameterBindings)
    await api.joinAnalysis(doc, 'target', parameterBindings)

    for (const path of ['/api/graph/schema', '/api/graph/estimate', '/api/graph/plan', '/api/graph/join-analysis']) {
      const call = fetchMock.mock.calls.find(([observed]) => observed === path)
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
        targetNodeId: 'target', parameterBindings,
      })
    }
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
