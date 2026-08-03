import { describe, it, expect, beforeEach, vi } from 'vitest'

// the store module runs autosave side-effects at import; stub the network client so nothing escapes.
// (Autosave is gated on _bootstrapped=false at import, so no PUT fires here anyway.)
const apiMocks = vi.hoisted(() => ({
  kernel: vi.fn(), nodes: vi.fn(), me: vi.fn(), users: vi.fn(),
  listCanvases: vi.fn(), listRuns: vi.fn(), getCanvas: vi.fn(), createCanvas: vi.fn(), saveCanvas: vi.fn(), deleteCanvas: vi.fn(), preview: vi.fn(),
  currentResults: vi.fn(), retainedResult: vi.fn(), retainedEditorPreview: vi.fn(), exampleRowsEditorPreview: vi.fn(),
  canvasTransformReferences: vi.fn(),
  resolveExampleSources: vi.fn(),
  estimate: vi.fn(), inputDrift: vi.fn(), run: vi.fn(), profileEstimate: vi.fn(), profileIdentity: vi.fn(), fullProfile: vi.fn(), runStatus: vi.fn(), cancelRun: vi.fn(),
  writeAdmission: vi.fn(),
  executionManifest: vi.fn(),
  activeRuns: vi.fn(), profileJobs: vi.fn(), workspaceJobs: vi.fn(), schema: vi.fn(), graphSizes: vi.fn(),
  promote: vi.fn(), processors: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_target, property) => property === 'kernel'
      ? apiMocks.kernel
      : property === 'nodes'
        ? apiMocks.nodes
        : property === 'me'
          ? apiMocks.me
          : property === 'users'
            ? apiMocks.users
            : property === 'listCanvases'
              ? apiMocks.listCanvases
      : property === 'listRuns'
        ? apiMocks.listRuns
      : property === 'retainedResult'
        ? apiMocks.retainedResult
      : property === 'currentResults'
        ? apiMocks.currentResults
      : property === 'retainedEditorPreview'
        ? apiMocks.retainedEditorPreview
      : property === 'exampleRowsEditorPreview'
        ? apiMocks.exampleRowsEditorPreview
      : property === 'getCanvas'
        ? apiMocks.getCanvas
        : property === 'canvasTransformReferences'
          ? apiMocks.canvasTransformReferences
        : property === 'resolveExampleSources'
          ? apiMocks.resolveExampleSources
        : property === 'createCanvas'
          ? apiMocks.createCanvas
          : property === 'saveCanvas'
            ? apiMocks.saveCanvas
          : property === 'deleteCanvas'
            ? apiMocks.deleteCanvas
          : property === 'preview'
            ? apiMocks.preview
            : property === 'estimate'
              ? apiMocks.estimate
              : property === 'writeAdmission'
                ? apiMocks.writeAdmission
              : property === 'executionManifest'
                ? apiMocks.executionManifest
              : property === 'inputDrift'
                ? apiMocks.inputDrift
              : property === 'run'
                ? apiMocks.run
              : property === 'profileEstimate'
                ? apiMocks.profileEstimate
              : property === 'profileIdentity'
                ? apiMocks.profileIdentity
              : property === 'fullProfile'
                ? apiMocks.fullProfile
                : property === 'runStatus'
                  ? apiMocks.runStatus
                  : property === 'cancelRun'
                    ? apiMocks.cancelRun
                    : property === 'activeRuns'
                    ? apiMocks.activeRuns
                    : property === 'profileJobs'
                      ? apiMocks.profileJobs
                      : property === 'workspaceJobs'
                        ? apiMocks.workspaceJobs
                      : property === 'promote'
                        ? apiMocks.promote
              : property === 'processors'
                ? apiMocks.processors
                : property === 'schema'
                  ? apiMocks.schema
                  : property === 'graphSizes'
                    ? apiMocks.graphSizes
          : async () => ({}),
  }),
  KernelError: class KernelError extends Error {
    status: number
    code?: string
    constructor(status: number, message: string, code?: string) { super(message); this.status = status; this.code = code }
  },
  setApiUser: vi.fn(),
}))

import {
  canvasViewportDocumentIdentity, currentPreviews, previewPlanIdentity, profileJobKey, profilePlanIdentity, useStore,
  writeAdmissionFingerprint,
} from './graph'
import { KernelError } from '../api/client'
import { register } from '../nodes/registry'
import type { CatalogTable, CanvasTransformReference } from '../types/api'
import type { CanvasDoc } from '../types/graph'
import {
  READ_ONLY_DRAFT_BASE_MESSAGE, UNAVAILABLE_DRAFT_BASE_MESSAGE, writeCanvasDraft,
} from './canvasDrafts'

const storage = new Map<string, string>()
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: {
    get length() { return storage.size },
    clear: () => storage.clear(),
    getItem: (key: string) => storage.get(key) ?? null,
    key: (index: number) => Array.from(storage.keys())[index] ?? null,
    removeItem: (key: string) => { storage.delete(key) },
    setItem: (key: string, value: string) => { storage.set(key, String(value)) },
  } satisfies Storage,
})

const NODE = (id: string, type = 'source') => ({
  id, type, position: { x: 0, y: 0 },
  data: { title: id, config: {}, status: 'draft' as const, history: [] },
})

const CURRENT_NODE = (id: string, type = 'source') => ({
  ...NODE(id, type),
  data: {
    ...NODE(id, type).data,
    status: 'latest' as const,
    lastRun: { rows: 10, ms: 25, placement: 'local' as const },
  },
})

const WRITE_RECEIPT = (revisionId: string, rows = 2) => ({
  datasetId: 'dataset-1',
  revisionId,
  name: 'output',
  parentHead: null,
  head: { datasetId: 'dataset-1', revisionId, retentionOwner: 'core' },
  rows,
  bytes: rows * 16,
  schema: [{ name: 'value', type: 'int64' }],
  partitions: [],
  publication: {
    provider: 'managed-local-file',
    logicalUri: 'managed://dataset-1',
    artifactUri: `/outputs/${revisionId}.parquet`,
    publishSequence: revisionId === 'revision-1' ? 1 : 2,
    idempotencyKey: `publish-${revisionId}`,
  },
  durable: true as const,
})

const WRITE_OUTPUT = (revisionId: string, rows = 2) => ({
  nodeId: 'write',
  portId: 'out',
  wire: 'dataset' as const,
  publicationKind: 'catalog' as const,
  outcome: 'committed' as const,
  rows,
  writeReceipt: WRITE_RECEIPT(revisionId, rows),
})

const WRITE_JOB = (canvasId: string, runId: string, revisionId = 'revision-1') => ({
  id: `t:${runId}`,
  runId,
  jobType: 'run' as const,
  status: 'done' as const,
  targetNodeId: 'write',
  outputs: [WRITE_OUTPUT(revisionId)],
  canvasId,
  canvasName: 'test',
  backend: 'local',
  placement: 'local' as const,
  attempt: runId,
  outputReceipt: WRITE_RECEIPT(revisionId),
})

describe('graph store — core authority ops', () => {
  beforeEach(() => {
    // start each test from a known empty doc
    localStorage.clear()
    apiMocks.kernel.mockReset().mockResolvedValue({})
    apiMocks.nodes.mockReset().mockResolvedValue([])
    apiMocks.me.mockReset().mockResolvedValue({ id: 'alice', name: 'Alice' })
    apiMocks.users.mockReset().mockResolvedValue([{ id: 'alice', name: 'Alice' }])
    apiMocks.listCanvases.mockReset().mockResolvedValue([])
    apiMocks.listRuns.mockReset().mockResolvedValue([])
    apiMocks.currentResults.mockReset().mockResolvedValue({
      latestNodeIds: [], failedNodeIds: [], staleNodeIds: [], unknownNodeIds: [], results: [],
    })
    apiMocks.retainedResult.mockReset().mockRejectedValue(new Error('no retained result'))
    apiMocks.retainedEditorPreview.mockReset()
    apiMocks.exampleRowsEditorPreview.mockReset()
    apiMocks.getCanvas.mockReset()
    apiMocks.canvasTransformReferences.mockReset().mockResolvedValue([])
    apiMocks.resolveExampleSources.mockReset().mockResolvedValue({ resolutions: [] })
    apiMocks.createCanvas.mockReset().mockImplementation(async (doc: { id: string }) => (
      { ok: true, id: doc.id, created: true }
    ))
    apiMocks.saveCanvas.mockReset().mockImplementation(async (_doc: unknown, _keepalive: boolean, expectedVersion?: number) => (
      { ok: true, id: 'c', version: (expectedVersion ?? 0) + 1 }
    ))
    apiMocks.deleteCanvas.mockReset().mockResolvedValue({ ok: true })
    apiMocks.preview.mockReset()
    apiMocks.estimate.mockReset().mockResolvedValue({ rows: 10, bytes: 100, placement: 'local', needsConfirm: false })
    apiMocks.writeAdmission.mockReset()
    apiMocks.executionManifest.mockReset()
    apiMocks.inputDrift.mockReset().mockResolvedValue({ drifted: false, sources: [] })
    apiMocks.run.mockReset().mockResolvedValue({
      runId: 'run-store-test', status: 'running', jobType: 'run', targetNodeId: 'target',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [], outputs: [],
    })
    apiMocks.profileEstimate.mockReset().mockResolvedValue({
      rows: 10, bytes: 100, placement: 'local', needsConfirm: false,
      targetPortId: 'out', planDigest: 'a'.repeat(64),
    })
    apiMocks.profileIdentity.mockReset().mockResolvedValue({
      targetPortId: 'out', planDigest: 'a'.repeat(64),
    })
    apiMocks.fullProfile.mockReset()
    apiMocks.runStatus.mockReset()
    apiMocks.cancelRun.mockReset().mockImplementation(async (runId: string) => ({
      runId, status: 'cancelled', jobType: 'run',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    }))
    apiMocks.activeRuns.mockReset().mockResolvedValue([])
    apiMocks.profileJobs.mockReset().mockResolvedValue([])
    apiMocks.workspaceJobs.mockReset().mockResolvedValue({ items: [], hasMore: false })
    apiMocks.schema.mockReset().mockResolvedValue({})
    apiMocks.graphSizes.mockReset().mockResolvedValue({})
    apiMocks.promote.mockReset()
    apiMocks.processors.mockReset().mockResolvedValue([])
    useStore.setState({ currentUser: { id: 'alice', name: 'Alice' } })
    useStore.setState({
      doc: { id: 'c', version: 1, name: 'test', nodes: [], edges: [], requirements: [] },
      canvasRole: 'owner', past: [], future: [], toasts: [], agentOpen: false, accessDenied: false, kernelUp: true,
      files: [{ id: 'c', name: 'test', version: 1, role: 'owner' }],
      profileJobs: {}, agentLog: [], localDrafts: [], draftStorageErrors: [], currentDraftId: null,
      serverVersion: 1, saved: true, viewportFitRequest: null, catalog: [],
    })
  })

  it('returns to the loaded Canvas without replacing its document and reveals a valid node', () => {
    const source = NODE('source', 'source')
    const liveDoc = { ...emptyTestDoc('c'), nodes: [source] }
    useStore.setState({
      doc: liveDoc,
      view: 'workspace',
      selectedId: null,
      selectedIds: [],
      nodeRevealRequest: null,
    })

    expect(useStore.getState().activateLoadedCanvasRoute('c', 'source')).toBe(true)

    const state = useStore.getState()
    expect(state.doc).toBe(liveDoc)
    expect(state.view).toBe('canvas')
    expect(state.selectedId).toBe('source')
    expect(state.selectedIds).toEqual(['source'])
    expect(state.nodeRevealRequest).toMatchObject({ canvasId: 'c', nodeId: 'source' })
    expect(state.toasts).toEqual([])
  })

  it('clears a stale return node while preserving the loaded Canvas document', () => {
    const liveDoc = { ...emptyTestDoc('c'), nodes: [NODE('source', 'source')] }
    useStore.setState({
      doc: liveDoc,
      view: 'workspace',
      selectedId: 'source',
      selectedIds: ['source'],
      nodeRevealRequest: { id: 1, canvasId: 'c', nodeId: 'source' },
    })

    expect(useStore.getState().activateLoadedCanvasRoute('c', 'deleted-node')).toBe(true)

    const state = useStore.getState()
    expect(state.doc).toBe(liveDoc)
    expect(state.view).toBe('canvas')
    expect(state.selectedId).toBeNull()
    expect(state.selectedIds).toEqual([])
    expect(state.nodeRevealRequest).toBeNull()
    expect(state.toasts.at(-1)?.msg).toBe('The requested node is no longer in this Canvas.')
  })

  it.each(['jobs', 'inbox'] as const)('returns from a Dataset viewer to %s atomically', (view) => {
    useStore.setState({
      view: 'workspace',
      workspaceResourceId: 'dataset:published',
      workspaceDatasetQuery: 'revision=rev-1&revisionDataset=published',
      jobsQuery: '',
      inboxQuery: '',
    })

    useStore.getState().returnFromWorkspaceDatasetViewer(view, 'filter=failed&run=run-1', 'dq=published')

    expect(useStore.getState()).toMatchObject({
      view,
      workspaceResourceId: null,
      workspaceDatasetQuery: 'dq=published',
      [view === 'jobs' ? 'jobsQuery' : 'inboxQuery']: 'filter=failed&run=run-1',
    })
  })

  it('drops a dataset revision query when opening a different Workspace resource', () => {
    useStore.setState({
      view: 'relationships',
      workspaceResourceId: 'dataset:published',
      workspaceDatasetQuery: 'revision=rev-1&revisionDataset=published&returnCanvas=canvas-1',
    })

    useStore.getState().setWorkspaceResource('dataset:events')

    expect(useStore.getState()).toMatchObject({
      view: 'workspace',
      workspaceResourceId: 'dataset:events',
      workspaceDatasetQuery: '',
    })
  })

  it('promotes same-title nodes with distinct stable identities and reuses one identity on retry', async () => {
    const transform = (id: string) => ({
      ...NODE(id, 'transform'),
      data: { ...NODE(id, 'transform').data, title: 'Same title', config: {
        mode: 'map', code: 'def fn(row): return row',
      } },
    })
    useStore.setState((state) => ({
      doc: { ...state.doc, id: 'stable-canvas', nodes: [transform('first'), transform('second')] },
    }))
    let attempt = 0
    apiMocks.promote.mockImplementation(async (body: { id: string; title: string; mode: string }) => {
      attempt += 1
      if (attempt === 1) throw new Error('response lost')
      return {
        id: `tr_${'a'.repeat(29)}`, version: 'v1', title: body.title, mode: body.mode,
        category: 'compute', inputColumns: [], inputSchema: [], outputSchema: [], requirements: [],
        paramsSchema: {}, previewable: true, blurb: '', provenance: 'promoted',
      }
    })

    await expect(useStore.getState().promote(
      'first', 'Normalize each row for reuse.',
    )).rejects.toThrow('response lost')
    await useStore.getState().promote('first', 'Normalize each row for reuse.')
    await useStore.getState().promote('second', 'Normalize the second input.')

    const keys = apiMocks.promote.mock.calls.map(([body]) => body.id)
    expect(keys[0]).toBe(keys[1])
    expect(keys[2]).not.toBe(keys[1])
    expect(keys.every((key) => key.length <= 256)).toBe(true)
    expect(apiMocks.promote.mock.calls.map(([body]) => body.blurb)).toEqual([
      'Normalize each row for reuse.',
      'Normalize each row for reuse.',
      'Normalize the second input.',
    ])
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

  it('retains agent placement ownership through a second Join input until the user drags', () => {
    const source = (id: string, y: number) => ({
      ...NODE(id), position: { x: 80, y },
    })
    const agentJoin = {
      ...NODE('agent-join', 'join'),
      position: { x: 432, y: 120 },
      data: { title: 'Agent join', config: {}, autoPlaced: true },
    }
    useStore.getState().applyAgentGraph({
      nodes: [source('events', 120), source('images', 480), agentJoin],
      edges: [{
        id: 'events-to-join', source: 'events', target: agentJoin.id,
        sourceHandle: 'out', targetHandle: 'a', data: { wire: 'dataset' },
      }],
    })

    useStore.getState().connect({
      id: 'images-to-join', source: 'images', target: agentJoin.id,
      sourceHandle: 'out', targetHandle: 'b', data: { wire: 'dataset' },
    })
    expect(useStore.getState().doc.nodes.find((node) => node.id === agentJoin.id)!.position)
      .toEqual({ x: 432, y: 300 })

    useStore.getState().removeEdge('images-to-join')
    useStore.getState().setNodes(useStore.getState().doc.nodes.map((node) => (
      node.id === agentJoin.id ? { ...node, position: { x: 960, y: 40 } } : node
    )))
    useStore.getState().updateData(agentJoin.id, { autoPlaced: false })
    useStore.getState().connect({
      id: 'images-to-dragged-join', source: 'images', target: agentJoin.id,
      sourceHandle: 'out', targetHandle: 'b', data: { wire: 'dataset' },
    })
    const dragged = useStore.getState().doc.nodes.find((node) => node.id === agentJoin.id)!
    expect(dragged.data.autoPlaced).toBe(false)
    expect(dragged.position).toEqual({ x: 960, y: 40 })
  })

  it('clears display-only agent requests when switching canvases', () => {
    useStore.setState({ agentLog: [{ role: 'user', text: 'previous canvas request' }] })

    useStore.getState().loadDoc({ id: 'other', version: 1, name: 'other', nodes: [], edges: [] }, 'owner')

    expect(useStore.getState().agentLog).toEqual([])
  })

  it('recovers missing Transform full-run bindings from the retained manifest on reload', async () => {
    const transform = {
      ...NODE('transform', 'transform'),
      data: { ...NODE('transform', 'transform').data, status: 'latest' as const, config: {
        mode: 'map', code: { parameterRef: 'transform_code' },
      } },
    }
    const doc = {
      id: 'c', version: 1, name: 'reopen', requirements: [],
      parameters: [{ name: 'transform_code', type: 'string' as const, required: true }],
      nodes: [transform], edges: [],
    }
    const parameterBindings = [{
      name: 'transform_code',
      value: "def fn(row): return {**row, 'derived': 1}",
    }]
    const identity = {
      runId: 'retained-transform-run',
      executionManifestSha256: 'a'.repeat(64),
      parameterBindings,
      output: {
        nodeId: 'transform', portId: 'out', wire: 'dataset',
        publicationKind: 'result', outcome: 'committed', uri: '/results/transform.parquet',
      },
    }
    apiMocks.currentResults.mockResolvedValueOnce({
      latestNodeIds: ['transform'], failedNodeIds: [], staleNodeIds: [], unknownNodeIds: [], results: [identity],
    })

    useStore.getState().loadDoc(doc, 'owner')

    await vi.waitFor(() => expect(useStore.getState().runs.transform?.parameterBindings)
      .toEqual(parameterBindings))
    expect(apiMocks.currentResults).toHaveBeenCalledWith(expect.objectContaining({ id: doc.id }))
    expect(useStore.getState().doc.nodes[0].data.status).toBe('latest')
    await vi.waitFor(() => expect(apiMocks.schema).toHaveBeenCalledWith(
      doc, undefined, undefined, parameterBindings,
    ))
  })

  it('preserves an existing local preview binding instead of replacing it with a full-run binding', () => {
    const transform = {
      ...NODE('transform', 'transform'),
      data: { ...NODE('transform', 'transform').data, status: 'latest' as const, config: {
        mode: 'map', code: { parameterRef: 'transform_code' },
      } },
    }
    const doc = {
      id: 'c', version: 1, name: 'reopen', requirements: [],
      parameters: [{ name: 'transform_code', type: 'string' as const, required: true }],
      nodes: [transform], edges: [],
    }
    const previewBindings = [{
      name: 'transform_code',
      value: "def fn(row): return {**row, 'preview_only': 1}",
    }]
    localStorage.setItem('dp-preview-bindings-alice-c', JSON.stringify({
      transform: {
        canvasId: 'c', nodeId: 'transform', portId: 'out',
        planIdentity: previewPlanIdentity(doc, 'transform', 'out'),
        parameterBindings: previewBindings, inputManifest: [],
      },
    }))

    useStore.getState().loadDoc(doc, 'owner')

    expect(useStore.getState().runs.transform?.parameterBindings).toEqual(previewBindings)
    expect(apiMocks.retainedResult).not.toHaveBeenCalled()
  })

  it('keeps an empty default preview binding authoritative over an older override after reload', () => {
    const transform = {
      ...NODE('transform', 'transform'),
      data: { ...NODE('transform', 'transform').data, status: 'latest' as const, config: {
        mode: 'map', code: { parameterRef: 'transform_code' },
      } },
    }
    const doc = {
      id: 'c', version: 1, name: 'reopen defaults', requirements: [],
      parameters: [{
        name: 'transform_code', type: 'string' as const,
        default: "def fn(row): return {**row, 'from_default': 1}",
      }],
      nodes: [transform], edges: [],
    }
    localStorage.setItem('dp-preview-bindings-alice-c', JSON.stringify({
      transform: {
        canvasId: 'c', nodeId: 'transform', portId: 'out',
        planIdentity: previewPlanIdentity(doc, 'transform', 'out'),
        // The user returned from an override full run to the Canvas default preview.
        parameterBindings: [], inputManifest: [],
      },
    }))
    apiMocks.retainedResult.mockResolvedValueOnce({
      runId: 'older-override-run',
      executionManifestSha256: 'a'.repeat(64),
      parameterBindings: [{
        name: 'transform_code',
        value: "def fn(row): return {**row, 'from_override': 1}",
      }],
      output: {
        nodeId: 'transform', portId: 'out', wire: 'dataset',
        publicationKind: 'result', outcome: 'committed', uri: '/results/override.parquet',
      },
    })

    useStore.getState().loadDoc(doc, 'owner')

    expect(useStore.getState().runs.transform?.parameterBindings).toEqual([])
    expect(useStore.getState().previewBindings.transform.parameterBindings).toEqual([])
    expect(apiMocks.retainedResult).not.toHaveBeenCalled()
  })

  it('does not overwrite a binding entered while retained-result recovery is in flight', async () => {
    const transform = {
      ...NODE('transform', 'transform'),
      data: { ...NODE('transform', 'transform').data, status: 'latest' as const, config: {
        mode: 'map', code: { parameterRef: 'transform_code' },
      } },
    }
    const doc = {
      id: 'c', version: 1, name: 'reopen', requirements: [],
      parameters: [{ name: 'transform_code', type: 'string' as const, required: true }],
      nodes: [transform], edges: [],
    }
    let finishRecovery!: (recovery: any) => void
    apiMocks.currentResults.mockImplementationOnce(() => new Promise((resolve) => {
      finishRecovery = resolve
    }))
    useStore.getState().loadDoc(doc, 'owner')
    await vi.waitFor(() => expect(apiMocks.currentResults).toHaveBeenCalled())
    const local = { name: 'transform_code', value: 'local edit' }
    useStore.getState().setRunParameterBinding('transform', local)

    const identity = {
      runId: 'older-retained-run', executionManifestSha256: 'a'.repeat(64),
      parameterBindings: [{ name: 'transform_code', value: 'retained code' }],
      output: {
        nodeId: 'transform', portId: 'out', wire: 'dataset',
        publicationKind: 'result', outcome: 'committed', uri: '/result.parquet',
      },
    }
    finishRecovery({
      latestNodeIds: ['transform'], failedNodeIds: [], staleNodeIds: [], unknownNodeIds: [], results: [identity],
    })

    await vi.waitFor(() => expect(useStore.getState().runs.transform?.parameterBindings)
      .toEqual([local]))
  })

  it('merges compatible Transform binding subsets and rejects conflicting values by name', async () => {
    const transforms = (['a', 'b'] as const).map((suffix) => ({
      ...NODE(`transform-${suffix}`, 'transform'),
      data: { ...NODE(`transform-${suffix}`, 'transform').data, status: 'latest' as const, config: {
        mode: 'map', code: { parameterRef: `code_${suffix}` },
      } },
    }))
    const doc = {
      id: 'c', version: 1, name: 'conflicting bindings', requirements: [],
      parameters: (['a', 'b'] as const).map((suffix) => ({
        name: `code_${suffix}`, type: 'string' as const, required: true,
      })),
      nodes: transforms, edges: [],
    }
    apiMocks.schema.mockClear()
    apiMocks.graphSizes.mockClear()
    useStore.setState({
      doc,
      schemas: { stale: { out: [{ name: 'wrong', type: 'string' }] } },
      sizes: { stale: { rows: 1, confidence: 'exact' } },
      runs: {
        'transform-a': { phase: 'idle', parameterBindings: [{ name: 'code_a', value: 'code a' }] },
        'transform-b': { phase: 'idle', parameterBindings: [{ name: 'code_b', value: 'code b' }] },
      },
    } as any)

    await useStore.getState().refreshSchemas()

    const mergedBindings = [
      { name: 'code_a', value: 'code a' },
      { name: 'code_b', value: 'code b' },
    ]
    expect(apiMocks.schema).toHaveBeenCalledWith(
      doc, undefined, undefined, mergedBindings,
    )
    expect(apiMocks.graphSizes).toHaveBeenCalledWith(doc, undefined, mergedBindings)

    apiMocks.schema.mockClear()
    apiMocks.graphSizes.mockClear()
    const sharedDoc = {
      ...doc,
      parameters: [{ name: 'shared_code', type: 'string' as const, required: true }],
      nodes: transforms.map((node) => ({
        ...node,
        data: { ...node.data, config: {
          ...node.data.config, code: { parameterRef: 'shared_code' },
        } },
      })),
    }
    useStore.setState({
      doc: sharedDoc,
      schemas: { stale: { out: [{ name: 'wrong', type: 'string' }] } },
      sizes: { stale: { rows: 1, confidence: 'exact' } },
      runs: {
        'transform-a': { phase: 'idle', parameterBindings: [{
          name: 'shared_code', value: 'code a',
        }] },
        'transform-b': { phase: 'idle', parameterBindings: [{
          name: 'shared_code', value: 'code b',
        }] },
      },
    } as any)

    await useStore.getState().refreshSchemas()

    expect(useStore.getState().schemas).toEqual({})
    expect(useStore.getState().sizes).toEqual({})
    expect(apiMocks.schema).not.toHaveBeenCalled()
    expect(apiMocks.graphSizes).not.toHaveBeenCalled()
  })

  it('checks persisted execution badges on reopen and settles them from server evidence', async () => {
    const snapshot = {
      id: 'c', version: 1, name: 'restored', requirements: [], edges: [], nodes: [
        { ...NODE('queued'), data: { ...NODE('queued').data, status: 'queued' as const } },
        { ...NODE('running'), data: { ...NODE('running').data, status: 'running' as const } },
        { ...NODE('draft'), data: { ...NODE('draft').data, status: 'draft' as const } },
        { ...NODE('latest'), data: { ...NODE('latest').data, status: 'latest' as const } },
        { ...NODE('stale'), data: { ...NODE('stale').data, status: 'stale' as const } },
        { ...NODE('failed'), data: { ...NODE('failed').data, status: 'failed' as const } },
      ],
    }

    useStore.getState().loadDoc(snapshot, 'owner') // ordinary reopen
    expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual([
      'checking', 'checking', 'draft', 'checking', 'stale', 'checking',
    ])
    await vi.waitFor(() => expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual([
      'stale', 'stale', 'draft', 'stale', 'stale', 'stale',
    ]))
    // The returned snapshot is not mutated; the same boundary can safely receive it from restore.
    expect(snapshot.nodes.map((node) => node.data.status)).toEqual([
      'queued', 'running', 'draft', 'latest', 'stale', 'failed',
    ])

    useStore.getState().loadDoc(snapshot, 'owner') // VersionHistoryModal restore
    expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual([
      'checking', 'checking', 'draft', 'checking', 'stale', 'checking',
    ])
    await vi.waitFor(() => expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual([
      'stale', 'stale', 'draft', 'stale', 'stale', 'stale',
    ]))
    await vi.waitFor(() => expect(apiMocks.activeRuns).toHaveBeenCalledTimes(2))
  })

  it('rechecks an unknown persisted badge and preserves uncertainty after a transient storage check', async () => {
    let finishRecovery!: (recovery: any) => void
    apiMocks.currentResults.mockImplementationOnce(() => new Promise((resolve) => {
      finishRecovery = resolve
    }))
    const snapshot = {
      id: 'c', version: 1, name: 'reopen unknown', requirements: [], edges: [], nodes: [
        { ...NODE('source'), data: { ...NODE('source').data, status: 'unknown' as const } },
      ],
    }

    useStore.getState().loadDoc(snapshot, 'owner')
    expect(useStore.getState().doc.nodes[0].data.status).toBe('checking')
    finishRecovery({
      latestNodeIds: [], failedNodeIds: [], staleNodeIds: [], unknownNodeIds: ['source'], results: [],
    })

    await vi.waitFor(() => expect(useStore.getState().doc.nodes[0].data.status).toBe('unknown'))
  })

  it('lets a delayed authoritative active-run response replace settled badges', async () => {
    let finishActive!: (statuses: any[]) => void
    apiMocks.activeRuns.mockImplementationOnce(() => new Promise((resolve) => { finishActive = resolve }))
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))
    const snapshot = {
      id: 'c', version: 1, name: 'reopen', requirements: [], edges: [], nodes: [
        { ...NODE('source'), data: { ...NODE('source').data, status: 'queued' as const } },
        { ...NODE('target', 'filter'), data: { ...NODE('target', 'filter').data, status: 'running' as const } },
      ],
    }

    useStore.getState().loadDoc(snapshot, 'owner')
    expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual(['checking', 'checking'])

    finishActive([{
      runId: 'live-recovered-run', status: 'running', jobType: 'run', targetNodeId: 'target',
      rowsProcessed: 1, ms: 10, placement: 'local', outputs: [],
      // Durable recovery may know the target + overall status before a target per-node entry exists.
      perNode: [{ nodeId: 'source', status: 'queued' }],
    }])

    await vi.waitFor(() => expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual([
      'queued', 'running',
    ]))
    expect(useStore.getState().runs.target?.status?.runId).toBe('live-recovered-run')
  })

  it('does not let a delayed reattach repaint a newer canvas after navigation', async () => {
    let finishActive!: (statuses: any[]) => void
    apiMocks.activeRuns.mockImplementationOnce(() => new Promise((resolve) => { finishActive = resolve }))
    const oldSnapshot = {
      id: 'old', version: 1, name: 'old', requirements: [], edges: [], nodes: [
        { ...NODE('old-node'), data: { ...NODE('old-node').data, status: 'running' as const } },
      ],
    }
    const next = { id: 'next', version: 1, name: 'next', requirements: [], edges: [], nodes: [NODE('next-node')] }

    useStore.getState().loadDoc(oldSnapshot, 'owner')
    useStore.getState().loadDoc(next, 'owner')
    finishActive([{
      runId: 'late-old-run', status: 'running', jobType: 'run', targetNodeId: 'old-node',
      rowsProcessed: 1, ms: 10, placement: 'local', outputs: [],
      perNode: [{ nodeId: 'old-node', status: 'running' }],
    }])

    await Promise.resolve()
    expect(useStore.getState().doc.id).toBe('next')
    expect(useStore.getState().doc.nodes[0].data.status).toBe('draft')
    expect(useStore.getState().runs['old-node']).toBeUndefined()
  })

  it('undo restores the pre-apply doc', () => {
    useStore.getState().applyAgentGraph({ nodes: [NODE('a')], edges: [] })
    expect(useStore.getState().doc.nodes).toHaveLength(1)
    useStore.getState().undo()
    expect(useStore.getState().doc.nodes).toHaveLength(0)  // back to the empty baseline
  })

  it('makes each edge add and selected-edge deletion one undoable action', () => {
    const first = { id: 'first', source: 'a', target: 'b', data: { wire: 'dataset' as const } }
    const selfLoop = { id: 'self-loop', source: 'a', target: 'a', data: { wire: 'dataset' as const } }
    useStore.setState((state) => ({
      doc: { ...state.doc, nodes: [NODE('a'), NODE('b')], edges: [] },
    }))

    useStore.getState().connect(first)
    useStore.getState().connect(selfLoop)
    expect(useStore.getState().past).toHaveLength(2)
    expect(useStore.getState().doc.edges.map((edge) => edge.id)).toEqual(['first', 'self-loop'])

    useStore.getState().undo()
    expect(useStore.getState().doc.edges.map((edge) => edge.id)).toEqual(['first'])
    useStore.getState().redo()
    expect(useStore.getState().doc.edges.map((edge) => edge.id)).toEqual(['first', 'self-loop'])

    useStore.setState({ selectedIds: ['self-loop'], selectedId: 'self-loop' })
    useStore.getState().removeSelected()
    expect(useStore.getState().past).toHaveLength(3)
    expect(useStore.getState().doc.edges.map((edge) => edge.id)).toEqual(['first'])

    useStore.getState().undo()
    expect(useStore.getState().doc.edges.map((edge) => edge.id)).toEqual(['first', 'self-loop'])
    useStore.getState().redo()
    expect(useStore.getState().doc.edges.map((edge) => edge.id)).toEqual(['first'])
  })

  it('marks the surviving downstream cone stale when an edge is removed', () => {
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [
          CURRENT_NODE('source'), CURRENT_NODE('target', 'filter'),
          CURRENT_NODE('downstream', 'write'), CURRENT_NODE('unrelated'),
        ],
        edges: [
          { id: 'source-target', source: 'source', target: 'target' },
          { id: 'target-downstream', source: 'target', target: 'downstream' },
        ],
      },
    }))

    useStore.getState().removeEdge('source-target')

    expect(useStore.getState().doc.nodes.map((node) => [node.id, node.data.status])).toEqual([
      ['source', 'latest'],
      ['target', 'stale'],
      ['downstream', 'stale'],
      ['unrelated', 'latest'],
    ])
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'target')?.data.lastRun)
      .toEqual({ rows: 10, ms: 25, placement: 'local' })
  })

  it('marks surviving descendants stale when an upstream node is removed', () => {
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [
          CURRENT_NODE('source'), CURRENT_NODE('target', 'filter'),
          CURRENT_NODE('downstream', 'write'), CURRENT_NODE('unrelated'),
        ],
        edges: [
          { id: 'source-target', source: 'source', target: 'target' },
          { id: 'target-downstream', source: 'target', target: 'downstream' },
        ],
      },
    }))

    useStore.getState().removeNode('source')

    expect(useStore.getState().doc.nodes.map((node) => [node.id, node.data.status])).toEqual([
      ['target', 'stale'],
      ['downstream', 'stale'],
      ['unrelated', 'latest'],
    ])
  })

  it.each([
    ['selected upstream node', ['source']],
    ['selected input edge', ['source-target']],
  ])('marks surviving descendants stale after deleting a %s', (_label, selectedIds) => {
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [
          CURRENT_NODE('source'), CURRENT_NODE('target', 'filter'),
          CURRENT_NODE('downstream', 'write'), CURRENT_NODE('unrelated'),
        ],
        edges: [
          { id: 'source-target', source: 'source', target: 'target' },
          { id: 'target-downstream', source: 'target', target: 'downstream' },
        ],
      },
      selectedIds,
      selectedId: selectedIds[0],
    }))

    useStore.getState().removeSelected()

    expect(useStore.getState().doc.nodes.find((node) => node.id === 'target')?.data.status)
      .toBe('stale')
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'downstream')?.data.status)
      .toBe('stale')
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'unrelated')?.data.status)
      .toBe('latest')
  })

  it.each(['bypass', 'disable'] as const)(
    'marks the toggled node and downstream results stale after %s changes execution semantics',
    (operation) => {
      useStore.setState((state) => ({
        doc: {
          ...state.doc,
          nodes: [
            CURRENT_NODE('source'), CURRENT_NODE('transform', 'filter'),
            CURRENT_NODE('downstream', 'write'), CURRENT_NODE('unrelated'),
          ],
          edges: [
            { id: 'source-transform', source: 'source', target: 'transform' },
            { id: 'transform-downstream', source: 'transform', target: 'downstream' },
          ],
        },
      }))

      useStore.getState()[operation]('transform')

      expect(useStore.getState().doc.nodes.map((node) => [node.id, node.data.status])).toEqual([
        ['source', 'latest'],
        ['transform', 'stale'],
        ['downstream', 'stale'],
        ['unrelated', 'latest'],
      ])
      const toggled = useStore.getState().doc.nodes.find((node) => node.id === 'transform')
      expect(operation === 'bypass' ? toggled?.data.bypassed : toggled?.data.disabled).toBe(true)
    },
  )

  it('restores the exact output identity when successive versions share a config', () => {
    const target = NODE('target', 'filter')
    const config = { predicate: 'score > 0' }
    target.data.status = 'latest'
    target.data.config = { ...config }
    target.data.history = [
      { id: 'older', ts: 1, rows: 10, label: 'run · 10 rows', config: { ...config } },
      { id: 'newer', ts: 2, rows: 20, label: 'run · 20 rows', config: { ...config } },
    ]
    target.data.currentOutputVersionId = 'newer'
    useStore.setState((state) => ({
      canvasRole: 'owner',
      doc: { ...state.doc, nodes: [target], edges: [] },
    }))

    useStore.getState().restoreVersion('target', 'older')

    expect(useStore.getState().doc.nodes[0].data).toMatchObject({
      status: 'latest',
      config,
      currentOutputVersionId: 'older',
    })
  })

  it('keeps menu-created node and edge in the same undo action', () => {
    register({
      kind: 'history-auto-source', title: 'History source', category: 'io',
      inputs: [], outputs: [{ id: 'out', wire: 'dataset' }],
      canBypass: false, blurb: '',
      defaultData: () => ({ title: 'History source', config: {}, status: 'draft', history: [] }),
    }, () => null)
    register({
      kind: 'history-auto-node', title: 'History node', category: 'compute',
      inputs: [{ id: 'in', wire: 'dataset' }], outputs: [{ id: 'out', wire: 'dataset' }],
      canBypass: false, blurb: '',
      defaultData: () => ({ title: 'History node', config: {}, status: 'draft', history: [] }),
    }, () => null)
    useStore.setState((state) => ({
      doc: { ...state.doc, nodes: [NODE('source', 'history-auto-source')], edges: [] },
    }))

    const node = useStore.getState().addConnectedNode('history-auto-node', { x: 100, y: 0 }, {
      source: 'source', sourceHandle: 'out', targetHandle: 'in', wire: 'dataset',
    })
    expect(node).not.toBeNull()

    expect(useStore.getState().past).toHaveLength(1)
    expect(useStore.getState().doc.nodes).toHaveLength(2)
    expect(useStore.getState().doc.edges).toHaveLength(1)

    useStore.getState().undo()
    expect(useStore.getState().doc.nodes.map((item) => item.id)).toEqual(['source'])
    expect(useStore.getState().doc.edges).toHaveLength(0)
    useStore.getState().redo()
    expect(useStore.getState().doc.nodes).toHaveLength(2)
    expect(useStore.getState().doc.edges).toHaveLength(1)
  })

  it('places connected insertions rightward and only recenters an un-dragged Join', () => {
    register({
      kind: 'topology-source', title: 'Topology source', category: 'io',
      inputs: [], outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false, blurb: '',
      defaultData: () => ({ title: 'Topology source', config: {}, status: 'draft', history: [] }),
    }, () => null)
    register({
      kind: 'topology-join', title: 'Topology join', category: 'compute',
      inputs: [{ id: 'a', wire: 'dataset' }, { id: 'b', wire: 'dataset' }],
      outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false, blurb: '',
      defaultData: () => ({ title: 'Topology join', config: {}, status: 'draft', history: [] }),
    }, () => null)
    const source = (id: string, x: number, y: number) => ({
      ...NODE(id, 'topology-source'), position: { x, y },
    })
    useStore.setState((state) => ({
      doc: { ...state.doc, nodes: [source('events', 80, 120), source('images', 80, 480)], edges: [] },
    }))

    const join = useStore.getState().addConnectedNode('topology-join', { x: -500, y: -500 }, {
      source: 'events', sourceHandle: 'out', targetHandle: 'a', wire: 'dataset',
    })!
    expect(join.position).toEqual({ x: 432, y: 120 })

    useStore.getState().connect({
      id: 'images-to-join', source: 'images', target: join.id,
      sourceHandle: 'out', targetHandle: 'b', data: { wire: 'dataset' },
    })
    expect(useStore.getState().doc.nodes.find((node) => node.id === join.id)!.position)
      .toEqual({ x: 432, y: 300 })
    expect(useStore.getState().doc.nodes.filter((node) => node.type === 'topology-source').map((node) => node.position))
      .toEqual([{ x: 80, y: 120 }, { x: 80, y: 480 }])

    // A settled drag opts out before a later wire is added; existing hand placement stays exact.
    useStore.getState().setNodes(useStore.getState().doc.nodes.map((node) => (
      node.id === join.id ? { ...node, position: { x: 960, y: 40 } } : node
    )))
    useStore.getState().updateData(join.id, { autoPlaced: false })
    expect(useStore.getState().doc.nodes.find((node) => node.id === join.id)!.data.autoPlaced).toBe(false)
    useStore.getState().removeEdge('images-to-join')
    useStore.getState().connect({
      id: 'images-to-join-again', source: 'images', target: join.id,
      sourceHandle: 'out', targetHandle: 'b', data: { wire: 'dataset' },
    })
    expect(useStore.getState().doc.nodes.find((node) => node.id === join.id)!.position)
      .toEqual({ x: 960, y: 40 })
  })

  it('separates only auto-placed same-lane Join inputs before centering the target', () => {
    const source = (id: string, x: number, autoPlaced: boolean) => ({
      ...NODE(id), position: { x, y: 120 }, data: { ...NODE(id).data, autoPlaced },
    })
    const join = {
      ...NODE('join', 'join'), position: { x: 792, y: 120 },
      data: { ...NODE('join', 'join').data, autoPlaced: true },
    }
    const firstEdge = {
      id: 'first', source: 'left', target: 'join',
      sourceHandle: 'out', targetHandle: 'a', data: { wire: 'dataset' as const },
    }
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [source('left', 80, true), source('right', 440, true), join],
        edges: [firstEdge],
      },
    }))
    useStore.getState().connect({
      id: 'second', source: 'right', target: 'join',
      sourceHandle: 'out', targetHandle: 'b', data: { wire: 'dataset' },
    })
    expect(useStore.getState().doc.nodes.map((node) => [node.id, node.position])).toEqual([
      ['left', { x: 80, y: 120 }],
      ['right', { x: 440, y: 400 }],
      ['join', { x: 792, y: 260 }],
    ])

    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [source('left', 80, true), source('right', 440, false), join],
        edges: [firstEdge],
      },
    }))
    useStore.getState().connect({
      id: 'manual-second', source: 'right', target: 'join',
      sourceHandle: 'out', targetHandle: 'b', data: { wire: 'dataset' },
    })
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'right')!.position)
      .toEqual({ x: 440, y: 120 })
  })

  it('reconnects an edge as one undoable action while retaining its stable identity', () => {
    const latest = (id: string) => ({
      ...NODE(id),
      data: { ...NODE(id).data, status: 'latest' as const },
    })
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [latest('source'), latest('old-target'), latest('new-target'), latest('downstream')],
        edges: [
          { id: 'rerouted', source: 'source', target: 'old-target', data: { wire: 'dataset' } },
          { id: 'downstream-edge', source: 'new-target', target: 'downstream', data: { wire: 'dataset' } },
        ],
      },
    }))

    useStore.getState().reconnectEdge('rerouted', {
      id: 'discarded-replacement-id',
      source: 'source',
      target: 'new-target',
      data: { wire: 'dataset' },
    })

    expect(useStore.getState().past).toHaveLength(1)
    expect(useStore.getState().doc.edges[0]).toMatchObject({
      id: 'rerouted', source: 'source', target: 'new-target',
    })
    expect(useStore.getState().doc.nodes.map((node) => [node.id, node.data.status])).toEqual([
      ['source', 'latest'],
      ['old-target', 'stale'],
      ['new-target', 'stale'],
      ['downstream', 'stale'],
    ])

    useStore.getState().undo()
    expect(useStore.getState().doc.edges[0]).toMatchObject({
      id: 'rerouted', source: 'source', target: 'old-target',
    })
    expect(useStore.getState().future).toHaveLength(1)

    useStore.getState().redo()
    expect(useStore.getState().doc.edges[0]).toMatchObject({
      id: 'rerouted', source: 'source', target: 'new-target',
    })
    expect(useStore.getState().past).toHaveLength(1)
  })

  it('keeps a running managed Write identity across an upstream edge edit', () => {
    const source = NODE('source')
    const write = NODE('write', 'write')
    const admission = {
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet',
      mode: 'create' as const, provider: 'managed-local-file', expectedSchema: [], partitions: [],
      intent: { idempotencyKey: 'managed-write-key' },
    }
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
        edges: [{ id: 'source-write', source: 'source', target: 'write' }],
      },
      runs: { write: {
        phase: 'running', writeAdmission: admission, writeSubmissionId: 'managed-submission',
        writeAdmissionFingerprint: 'admitted-graph',
      } },
    } as any)

    useStore.getState().removeEdge('source-write')

    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'running', writeAdmission: admission, writeSubmissionId: 'managed-submission',
      writeAdmissionFingerprint: 'admitted-graph',
    })
  })

  it('identifies Write admission by target status, edge id, parameters, bindings, and inputs', () => {
    const source = NODE('source')
    source.data.status = 'latest'
    const write = NODE('write', 'write')
    write.data.config = {
      filename: { parameterRef: 'output' }, writeMode: { parameterRef: 'mode' },
    }
    const unrelated = NODE('unrelated', 'filter')
    unrelated.data.config = { threshold: { parameterRef: 'unused' } }
    const parameters = [
      { name: 'output', type: 'string' as const, default: 'first', label: 'Output', constraints: { minLength: 1 } },
      { name: 'mode', type: 'string' as const, default: 'overwrite' },
      { name: 'unused', type: 'integer' as const, default: 10 },
    ]
    const bindings = [{ name: 'output', value: 'result' }, { name: 'mode', value: 'overwrite' }]
    const manifest = [{
      node_id: 'source', dataset_id: 'dataset', revision_id: '1', provider: 'local', resolved_at: 'now',
    }]
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], parameters,
      nodes: [source, write, unrelated], edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    const initial = writeAdmissionFingerprint(doc, 'write', bindings, manifest)
    const presentationOnly = {
      ...doc,
      parameters: [
        parameters[1], { ...parameters[0], label: 'Renamed for display' },
        { ...parameters[2], default: 999 },
      ],
      nodes: doc.nodes.map((node) => node.id === 'write'
        ? {
            ...node, position: { x: 400, y: 200 },
            data: {
              ...node.data, history: [{ label: 'run · 1 output' }],
              lastRun: { outputCount: 1, ms: 12, placement: 'local' },
            },
          }
        : node.id === 'unrelated'
          ? { ...node, data: { ...node.data, config: { threshold: 999 } } }
          : node),
    }
    expect(writeAdmissionFingerprint(
      presentationOnly, 'write', [bindings[1], bindings[0]],
      [{ ...manifest[0], resolved_at: 'later' }],
    )).toBe(initial)

    const withStatusChange = {
      ...doc,
      nodes: doc.nodes.map((node) => node.id === 'source'
        ? { ...node, data: { ...node.data, status: 'stale' as const } } : node),
    }
    expect(writeAdmissionFingerprint(withStatusChange, 'write', bindings, manifest)).not.toBe(initial)
    expect(writeAdmissionFingerprint(
      { ...doc, executionBackend: 'local-subprocess' }, 'write', bindings, manifest,
    )).not.toBe(initial)
    expect(writeAdmissionFingerprint(
      { ...doc, edges: [{ ...doc.edges[0], id: 'replacement-id' }] },
      'write', bindings, manifest,
    )).not.toBe(initial)
    expect(writeAdmissionFingerprint(
      {
        ...doc,
        parameters: parameters.map((parameter) => parameter.name === 'output'
          ? { ...parameter, default: 'second' } : parameter),
      },
      'write', bindings, manifest,
    )).not.toBe(initial)
    expect(writeAdmissionFingerprint(
      doc, 'write', [bindings[1], { name: 'output', value: 'other' }], manifest,
    )).not.toBe(initial)
    expect(writeAdmissionFingerprint(
      doc, 'write', bindings, [{ ...manifest[0], revision_id: '2' }],
    )).not.toBe(initial)
  })

  it('preserves union edge and admitted input order in Write admission identity', () => {
    const sourceA = NODE('source-a')
    const sourceB = NODE('source-b')
    const union = NODE('union', 'union')
    const write = NODE('write', 'write')
    const edges = [
      { id: 'a-union', source: 'source-a', target: 'union' },
      { id: 'b-union', source: 'source-b', target: 'union' },
      { id: 'union-write', source: 'union', target: 'write' },
    ]
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [],
      nodes: [sourceA, sourceB, union, write], edges,
    }
    const manifest = [
      { node_id: 'source-a', dataset_id: 'a', revision_id: '1', provider: 'local', resolved_at: 'first' },
      { node_id: 'source-b', dataset_id: 'b', revision_id: '1', provider: 'local', resolved_at: 'second' },
    ]
    const initial = writeAdmissionFingerprint(doc, 'write', undefined, manifest)

    expect(writeAdmissionFingerprint(
      { ...doc, edges: [edges[1], edges[0], edges[2]] }, 'write', undefined, manifest,
    )).not.toBe(initial)
    expect(writeAdmissionFingerprint(
      doc, 'write', undefined, [manifest[1], manifest[0]],
    )).not.toBe(initial)
    expect(writeAdmissionFingerprint(
      doc, 'write', undefined, manifest.map((item) => ({ ...item, resolved_at: 'later' })),
    )).toBe(initial)
  })

  it('does not treat a non-Section config.outputs field as a port declaration', () => {
    const plugin = NODE('plugin', 'configured-plugin')
    const sink = NODE('sink', 'write')
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [], nodes: [plugin, sink],
        edges: [{
          id: 'plugin-sink', source: 'plugin', sourceHandle: 'declared',
          target: 'sink', targetHandle: 'in', data: { wire: 'dataset' },
        }],
      },
    })

    useStore.getState().updateConfig('plugin', { outputs: ['unrelated-config-value'] })
    expect(useStore.getState().doc.edges.map((edge) => edge.id)).toEqual(['plugin-sink'])
  })

  it('binds an implicit Section edge to its former sole port when outputs become named', () => {
    const section = NODE('section', 'section')
    section.data.config = { outputs: ['out'] }
    const keep = NODE('keep', 'write')
    const drop = NODE('drop', 'write')
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [], nodes: [section, keep, drop],
        edges: [
          { id: 'implicit', source: 'section', target: 'keep', data: { wire: 'dataset' } },
          { id: 'removed', source: 'section', sourceHandle: 'old', target: 'drop', data: { wire: 'dataset' } },
        ],
      },
    })

    useStore.getState().updateConfig('section', { outputs: ['left', 'out'] })

    expect(useStore.getState().doc.edges).toEqual([expect.objectContaining({
      id: 'implicit', sourceHandle: 'out',
    })])
  })

  it('clears stale size estimates for an edited Sample and its downstream cone only', () => {
    const latest = (id: string, type = 'source', config = {}) => ({
      ...NODE(id, type),
      data: { ...NODE(id, type).data, status: 'latest' as const, config },
    })
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [
          latest('source'),
          latest('sample', 'sample', { n: 1000, seed: 42 }),
          latest('transform', 'transform'),
          latest('unrelated'),
        ],
        edges: [
          { id: 'source-sample', source: 'source', target: 'sample', data: { wire: 'dataset' } },
          { id: 'sample-transform', source: 'sample', target: 'transform', data: { wire: 'dataset' } },
        ],
      },
      sizes: {
        source: { rows: 10_000, confidence: 'exact' },
        sample: { rows: 1_000, confidence: 'exact' },
        transform: { rows: 1_000, confidence: 'bounded' },
        unrelated: { rows: 500, confidence: 'exact' },
      },
    }))

    useStore.getState().updateConfig('sample', { n: 25 })

    expect(useStore.getState().sizes).toEqual({
      source: { rows: 10_000, confidence: 'exact' },
      unrelated: { rows: 500, confidence: 'exact' },
    })
    expect(useStore.getState().doc.nodes.map((node) => [node.id, node.data.status])).toEqual([
      ['source', 'latest'], ['sample', 'stale'], ['transform', 'stale'], ['unrelated', 'latest'],
    ])
  })

  it('loads unsupported historical shapes verbatim instead of silently migrating them', () => {
    const legacy = {
      id: 'legacy', version: 1, nodes: [{
        id: 'old', type: 'notebook', position: { x: 0, y: 0 },
        data: { title: 'old', status: 'draft', muted: true, config: {} },
      }], edges: [],
    }
    useStore.getState().loadDoc(legacy as any, 'owner')
    const node = useStore.getState().doc.nodes[0]
    expect(node.type).toBe('notebook')
    expect((node.data as any).muted).toBe(true)
  })

  it('binds a preview to its canvas and plan identity, then blocks a stale response', async () => {
    let finish!: (result: ReturnType<typeof previewResult>) => void
    apiMocks.preview.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [],
        nodes: [NODE('source'), NODE('filter', 'filter')],
        edges: [{ id: 'source-filter', source: 'source', target: 'filter', data: { wire: 'dataset' } }],
      },
    })

    const first = useStore.getState().runPreview('filter')
    const pending = useStore.getState().previews.filter
    expect(pending).toMatchObject({ canvasId: 'c', nodeId: 'filter', loading: true, offset: 0 })

    useStore.getState().updateConfig('source', { uri: 'new-events.parquet' })
    finish(previewResult('purchase'))
    await first

    expect(useStore.getState().previews.filter?.result).toBeUndefined()
    apiMocks.preview.mockResolvedValueOnce(previewResult('view'))
    await useStore.getState().runPreview('filter')
    expect(useStore.getState().previews.filter?.result?.rows).toEqual([{ value: 'view' }])
  })

  it('reports a non-previewable plugin locally without sending a preview request', async () => {
    register({
      kind: 'store-non-previewable-plugin', title: 'Full-pass plugin', category: 'compute',
      inputs: [], outputs: [{ id: 'out', label: 'Out', wire: 'dataset' }], canBypass: false,
      previewable: false, defaultData: () => ({ title: 'Full-pass plugin', config: {}, status: 'draft', history: [] }), blurb: '',
    }, () => null)
    useStore.setState({
      doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('plugin', 'store-non-previewable-plugin')], edges: [] },
    })

    await useStore.getState().runPreview('plugin')

    expect(apiMocks.preview).not.toHaveBeenCalled()
    expect(useStore.getState().previews.plugin?.result).toMatchObject({
      notPreviewable: true,
      reason: 'Full-pass plugin does not support bounded previews. Run this step to produce its result.',
      suggestedAction: 'run',
    })
  })

  it('blocks a stale sample response after its seed changes', async () => {
    let finish!: (result: ReturnType<typeof previewResult>) => void
    apiMocks.preview.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const source = NODE('source')
    source.data.config = { uri: 'events.parquet' }
    const sample = NODE('sample', 'sample')
    sample.data.config = { n: 100, seed: 42 }
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, sample],
        edges: [{ id: 'source-sample', source: 'source', target: 'sample', data: { wire: 'dataset' } }],
      },
    })

    const first = useStore.getState().runPreview('sample')
    useStore.getState().updateConfig('sample', { seed: 73 })
    finish(previewResult('old seed'))
    await first

    expect(useStore.getState().previews.sample?.result).toBeUndefined()
  })

  it('tests Transform code through the server-owned retained candidate without binding formal preview state', async () => {
    const source = NODE('source')
    source.data.config = { uri: 'events.parquet' }
    const sample = NODE('sample', 'sample')
    sample.data.config = { n: 25, seed: 42 }
    const transform = NODE('transform', 'transform')
    transform.data.config = { source: 'adhoc', mode: 'map', code: 'def fn(row): return row' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, sample, transform],
      edges: [
        { id: 'source-sample', source: 'source', target: 'sample', data: { wire: 'dataset' as const } },
        { id: 'sample-transform', source: 'sample', sourceHandle: 'out', target: 'transform', targetHandle: 'in', data: { wire: 'sample' as const } },
      ],
    }
    const retained = {
      ...previewResult('retained'),
      editorTestInput: {
        runId: 'retained-run', nodeId: 'sample', portId: 'out', label: 'sample', rows: 25,
      },
    }
    const formalPreview = { sample: { result: previewResult('formal') } }
    const formalBindings = { sample: { inputManifest: [{ node_id: 'source' }] } }
    apiMocks.retainedEditorPreview.mockResolvedValue(retained)
    useStore.setState({
      doc, editorPreviews: {}, previews: formalPreview, previewBindings: formalBindings,
    } as any)

    await useStore.getState().runEditorPreview('transform')

    expect(apiMocks.listRuns).not.toHaveBeenCalled()
    expect(apiMocks.retainedEditorPreview).toHaveBeenCalledWith(
      doc, 'transform', 50, 0, undefined, [],
    )
    expect(useStore.getState().editorPreviews.transform?.result).toEqual(retained)
    expect(useStore.getState().previews).toBe(formalPreview)
    expect(useStore.getState().previewBindings).toBe(formalBindings)
  })

  it('tests editor-local Example rows without an upstream or durable Canvas state', async () => {
    const transform = NODE('transform', 'transform')
    transform.data.config = {
      source: 'adhoc', mode: 'map', code: 'def fn(row): return row',
    }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [transform], edges: [],
    }
    const formalPreviews = { other: { result: previewResult('formal') } }
    const formalBindings = { other: { inputManifest: [{ node_id: 'source' }] } }
    const runs = { transform: { phase: 'idle' as const } }
    const structuredFailure = {
      columns: [], rows: [], truncated: false, completeness: 'unknown' as const,
      notPreviewable: false, error: true, failureCategory: 'user_code_exception' as const,
      reason: 'ValueError: fixture boom',
      userCodeException: {
        nodeId: 'transform', exceptionType: 'ValueError', message: 'fixture boom',
        availableColumns: ['value'],
      },
      wire: 'dataset',
    }
    apiMocks.exampleRowsEditorPreview.mockResolvedValue(structuredFailure)
    useStore.setState({
      doc, editorPreviews: {}, previews: formalPreviews,
      previewBindings: formalBindings, runs,
    } as any)

    await useStore.getState().runEditorExamplePreview(
      'transform', '[{"value":1}]',
    )

    expect(apiMocks.exampleRowsEditorPreview).toHaveBeenCalledWith(
      doc, 'transform', '[{"value":1}]', 50, 0, undefined, [],
    )
    expect(useStore.getState().editorPreviews.transform?.result).toEqual(structuredFailure)
    expect(useStore.getState().doc).toBe(doc)
    expect(useStore.getState().previews).toBe(formalPreviews)
    expect(useStore.getState().previewBindings).toBe(formalBindings)
    expect(useStore.getState().runs).toBe(runs)
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(apiMocks.listRuns).not.toHaveBeenCalled()
  })

  it('drops a late Example rows response after the editor fixture is cleared', async () => {
    let finish!: (result: ReturnType<typeof previewResult>) => void
    apiMocks.exampleRowsEditorPreview.mockImplementationOnce(
      () => new Promise((resolve) => { finish = resolve }),
    )
    const transform = NODE('transform', 'transform')
    transform.data.config = {
      source: 'adhoc', mode: 'map', code: 'def fn(row): return row',
    }
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [],
        nodes: [transform], edges: [],
      },
      editorPreviews: {},
    } as any)

    const pending = useStore.getState().runEditorExamplePreview(
      'transform', '[{"value":"old"}]',
    )
    useStore.getState().clearEditorPreview('transform')
    finish(previewResult('old fixture'))
    await pending

    expect(useStore.getState().editorPreviews.transform).toBeUndefined()
  })

  it('turns an explicit retained-input miss into the editor-only Run upstream state', async () => {
    const upstream = NODE('sample', 'sample')
    const transform = NODE('transform', 'transform')
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [upstream, transform],
      edges: [{
        id: 'sample-transform', source: 'sample', sourceHandle: 'out',
        target: 'transform', targetHandle: 'in', data: { wire: 'sample' as const },
      }],
    }
    apiMocks.retainedEditorPreview.mockRejectedValue(
      new KernelError(409, 'no current input', 'retained_upstream_stale'))
    useStore.setState({ doc, editorPreviews: {} })

    await useStore.getState().runEditorPreview('transform')

    expect(useStore.getState().editorPreviews.transform?.result).toMatchObject({
      notPreviewable: true,
      reason: 'No current retained sample result is available.',
    })
    expect(useStore.getState().previews.transform).toBeUndefined()
  })

  it('binds multi-output preview freshness to the selected port and preserves it on refresh', async () => {
    let finishPass!: (result: ReturnType<typeof previewResult>) => void
    let finishOut!: (result: ReturnType<typeof previewResult>) => void
    apiMocks.preview
      .mockResolvedValueOnce(previewResult('default out'))
      .mockImplementationOnce(() => new Promise((resolve) => { finishPass = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishOut = resolve }))
    const section = NODE('section', 'section')
    section.data.config = { outputs: ['pass', 'out'] }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [section], edges: [] }
    useStore.setState({ doc })

    expect(previewPlanIdentity(doc, 'section', 'pass')).not.toBe(previewPlanIdentity(doc, 'section', 'out'))
    await useStore.getState().runPreview('section')
    expect(apiMocks.preview).toHaveBeenLastCalledWith(doc, 'section', 50, 0, 'out')
    const pass = useStore.getState().runPreview('section', 0, 'pass')
    const out = useStore.getState().runPreview('section', 0, 'out')
    finishOut(previewResult('selected out'))
    await out
    finishPass(previewResult('stale pass'))
    await pass

    expect(useStore.getState().previews.section).toMatchObject({
      portId: 'out', result: previewResult('selected out'),
    })
    apiMocks.preview.mockResolvedValueOnce(previewResult('refreshed out'))
    await useStore.getState().runPreview('section')
    expect(apiMocks.preview).toHaveBeenLastCalledWith(doc, 'section', 50, 0, 'out')
    expect(useStore.getState().previews.section).toMatchObject({
      portId: 'out', result: previewResult('refreshed out'),
    })
    expect(currentPreviews(doc, useStore.getState().previews).section).toMatchObject({
      portId: 'out', result: previewResult('refreshed out'),
    })
  })

  it('sends multi-output full runs to the backend capability boundary from every entry point', async () => {
    const source = NODE('source')
    source.data.config = { uri: 'events.parquet' }
    const section = NODE('section', 'section')
    section.data.config = {
      outputs: ['left', 'right'], script: "emit(inputs['in'], 'left')", params: {}, maxRuns: 200,
    }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, section],
      edges: [{ id: 'source-section', source: 'source', target: 'section', data: { wire: 'dataset' as const } }],
    }
    useStore.setState({ doc })
    const running = {
      runId: 'multi-output-run', status: 'running', jobType: 'run', targetNodeId: 'section',
      rowsProcessed: 0, totalRows: null, ms: 0, placement: 'local', perNode: [],
      outputs: [
        { nodeId: 'section', portId: 'left', wire: 'dataset', publicationKind: 'result', outcome: 'pending' },
        { nodeId: 'section', portId: 'right', wire: 'dataset', publicationKind: 'result', outcome: 'pending' },
      ],
    }
    apiMocks.run.mockResolvedValue(running)
    apiMocks.runStatus.mockResolvedValue({
      ...running, status: 'done', ms: 25,
      outputs: [
        { ...running.outputs[0], outcome: 'committed', uri: '/outputs/left.parquet', rows: 4 },
        { ...running.outputs[1], outcome: 'committed', uri: '/outputs/right.parquet', rows: 6 },
      ],
    })

    await useStore.getState().requestRun('section')
    expect(apiMocks.estimate).toHaveBeenCalledWith(doc, 'section')
    expect(apiMocks.run).toHaveBeenCalledWith(
      expect.objectContaining({ id: doc.id }), 'section', false, expect.any(String),
    )
    await vi.waitFor(() => expect(useStore.getState().doc.nodes[1].data.status).toBe('latest'))

    apiMocks.estimate.mockClear()
    await useStore.getState().estimate('section')
    expect(apiMocks.estimate).toHaveBeenCalledWith(
      expect.objectContaining({ id: doc.id }), 'section',
    )

    apiMocks.run.mockClear()
    await useStore.getState().run('section')
    expect(apiMocks.run).toHaveBeenCalledWith(
      expect.objectContaining({ id: doc.id }), 'section', false, expect.any(String),
    )

    apiMocks.estimate.mockClear()
    useStore.getState().rerunAll()
    await vi.waitFor(() => expect(apiMocks.estimate).toHaveBeenCalledWith(
      expect.objectContaining({ id: doc.id }), 'section',
    ))
    expect(useStore.getState().toasts).toHaveLength(0)
  })

  it('blocks execution entry points while the hub is offline without mutating the graph', async () => {
    const source = NODE('source')
    source.data.config = { uri: 'events' }
    const doc = {
      id: 'c', version: 1, name: 'offline draft', requirements: [],
      nodes: [source], edges: [],
    }
    useStore.setState({ doc, kernelUp: false, toasts: [] })

    await useStore.getState().runPreview('source')
    await useStore.getState().requestRun('source')
    useStore.getState().rerunAll()

    expect(apiMocks.preview).not.toHaveBeenCalled()
    expect(apiMocks.estimate).not.toHaveBeenCalled()
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(useStore.getState().doc).toBe(doc)
    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error',
      msg: 'Data Playground is offline — reconnect before starting or controlling a run.',
      dedupeKey: 'hub-offline-execution',
    }])
  })

  it('keeps requestRun at the estimate panel when exact input registration is required', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/post-startup.parquet' }
    const target = NODE('target', 'filter')
    const doc = {
      id: 'c', version: 1, name: 'exact readiness', requirements: [], nodes: [source, target],
      edges: [{ id: 'source-target', source: 'source', target: 'target', data: { wire: 'dataset' as const } }],
    }
    useStore.setState({ doc, runs: {}, openPanels: {} })
    apiMocks.estimate.mockResolvedValueOnce({
      rows: 2, placement: 'local', needsConfirm: false,
      exactRunReadiness: {
        ready: false, reason: 'registration_required', sourceNodeIds: ['source'],
        message: 'Register this local input through the Source data picker before running.',
      },
    })

    await useStore.getState().requestRun('target')

    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(useStore.getState().runs.target).toMatchObject({
      phase: 'estimated', estimate: { exactRunReadiness: { ready: false } },
    })
    expect(useStore.getState().openPanels).toEqual({ target: 'run' })

    await useStore.getState().run('target')
    expect(apiMocks.run).not.toHaveBeenCalled()

    useStore.getState().updateConfig('source', {
      uri: '/data/post-startup.parquet',
      tableId: 'registered-post-startup',
    })
    expect(useStore.getState().runs.target).toMatchObject({ phase: 'idle' })
    expect(useStore.getState().runs.target.estimate).toBeUndefined()

    apiMocks.estimate.mockResolvedValueOnce({
      rows: 2, placement: 'local', needsConfirm: false,
      exactRunReadiness: { ready: true, reason: 'ready', sourceNodeIds: [] },
    })
    await useStore.getState().requestRun('target')

    expect(apiMocks.estimate).toHaveBeenLastCalledWith(
      expect.objectContaining({ nodes: expect.arrayContaining([
        expect.objectContaining({
          id: 'source',
          data: expect.objectContaining({
            config: expect.objectContaining({ tableId: 'registered-post-startup' }),
          }),
        }),
      ]) }),
      'target',
    )
    expect(apiMocks.run).toHaveBeenCalledWith(
      expect.objectContaining({ id: doc.id }), 'target', false, expect.any(String),
    )
  })

  const fanOutDoc = (sinks: number) => {
    const source = NODE('source')
    source.data.config = { uri: 'events' }
    const nodes = [source]
    const edges = []
    for (let i = 0; i < sinks; i += 1) {
      nodes.push(NODE(`sink${i}`, 'filter'))
      edges.push({
        id: `source-sink${i}`, source: 'source', target: `sink${i}`,
        data: { wire: 'dataset' as const },
      })
    }
    return { id: 'c', version: 1, name: 'fan out', requirements: [], nodes, edges }
  }

  it('rerun all dispatches one whole-graph run instead of one run per sink', async () => {
    const doc = fanOutDoc(8)
    useStore.setState({ doc, runs: {}, graphRun: null, toasts: [] })
    const perNode = doc.nodes.map((node) => ({ nodeId: node.id, status: 'done', ms: 3 }))
    apiMocks.run.mockResolvedValue({
      runId: 'graph-run', status: 'running', jobType: 'run', targetNodeId: null,
      rowsProcessed: 0, totalRows: null, ms: 0, placement: 'local', progress: 0,
      perNode: doc.nodes.map((node) => ({ nodeId: node.id, status: 'queued' })), outputs: [],
    })
    apiMocks.runStatus.mockResolvedValue({
      runId: 'graph-run', status: 'done', jobType: 'run', targetNodeId: null,
      rowsProcessed: 0, totalRows: null, ms: 40, placement: 'local', progress: 1,
      perNode, outputs: [],
    })

    useStore.getState().rerunAll()

    await vi.waitFor(() => expect(useStore.getState().graphRun).toBeNull())
    expect(apiMocks.run).toHaveBeenCalledTimes(1)
    expect(apiMocks.run).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'c' }), undefined, false, expect.any(String),
    )
    expect(apiMocks.estimate).not.toHaveBeenCalled()
    // the single pass reports every step, so each executed node ends with its own result readout
    expect(useStore.getState().doc.nodes.every((node) => node.data.status === 'latest')).toBe(true)
    expect(useStore.getState().doc.nodes.filter((node) => node.data.lastRun)).toHaveLength(9)
    expect(useStore.getState().toasts).toHaveLength(0)
  })

  it('lets the user stop the single whole-graph run', async () => {
    const doc = fanOutDoc(2)
    useStore.setState({
      doc, graphRun: {
        canvasId: doc.id, runId: 'graph-run', status: {
          runId: 'graph-run', status: 'running', jobType: 'run', targetNodeId: null,
          rowsProcessed: 0, ms: 4, placement: 'local', progress: 0.5,
          perNode: [
            { nodeId: 'source', status: 'done' },
            { nodeId: 'sink0', status: 'running' },
            { nodeId: 'sink1', status: 'queued' },
          ], outputs: [],
        },
      },
    })

    await useStore.getState().cancelGraphRun()

    expect(apiMocks.cancelRun).toHaveBeenCalledWith('graph-run')
    expect(useStore.getState().graphRun).toBeNull()
    expect(useStore.getState().doc.nodes.some((node) => node.data.status === 'running')).toBe(false)
  })

  it('keeps rerun all on per-sink dispatch when a sink publishes a Write', async () => {
    const doc = fanOutDoc(2)
    doc.nodes.push(NODE('publish', 'write'))
    doc.edges.push({ id: 'sink0-publish', source: 'sink0', target: 'publish', data: { wire: 'dataset' as const } })
    useStore.setState({ doc, runs: {}, graphRun: null, toasts: [] })
    apiMocks.estimate.mockResolvedValue({ rows: 5, placement: 'local', needsConfirm: false })
    apiMocks.runStatus.mockResolvedValue({
      runId: 'run-store-test', status: 'done', jobType: 'run', targetNodeId: 'sink1',
      rowsProcessed: 5, ms: 5, placement: 'local', perNode: [], outputs: [],
    })

    useStore.getState().rerunAll()

    await vi.waitFor(() => expect(useStore.getState().runs.sink1?.phase).toBe('done'))
    expect(apiMocks.estimate).toHaveBeenCalledTimes(2)
    expect(useStore.getState().graphRun).toBeNull()
    expect(apiMocks.run).not.toHaveBeenCalledWith(
      expect.anything(), undefined, expect.anything(), expect.anything(),
    )
  })

  it('falls back to per-sink dispatch when the whole-graph pass needs a size confirmation', async () => {
    const doc = fanOutDoc(3)
    useStore.setState({ doc, runs: {}, graphRun: null, toasts: [] })
    apiMocks.run.mockRejectedValue(new KernelError(
      409, 'run needs confirmation', 'run_confirmation_required'))
    apiMocks.estimate.mockResolvedValue({ rows: 5, placement: 'local', needsConfirm: true })

    useStore.getState().rerunAll()

    await vi.waitFor(() => expect(apiMocks.estimate).toHaveBeenCalledTimes(3))
    expect(apiMocks.run).toHaveBeenCalledTimes(1)
    expect(useStore.getState().graphRun).toBeNull()
  })

  it('explains why rerun all cannot start a legacy graph with no terminal sink', () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/events.lance' }
    const filter = NODE('filter', 'filter')
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, filter],
      edges: [
        { id: 'source-filter', source: 'source', target: 'filter', data: { wire: 'dataset' as const } },
        { id: 'filter-source', source: 'filter', target: 'source', data: { wire: 'dataset' as const } },
      ],
    }
    useStore.setState({ doc })

    useStore.getState().rerunAll()

    expect(apiMocks.estimate).not.toHaveBeenCalled()
    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error', msg: 'Cannot rerun: graph has a cycle. Remove it or use a Section for control flow.',
    }])
  })

  it('explains the actionable Join condition error when rerun all refuses dispatch', () => {
    const left = NODE('left', 'source')
    const right = NODE('right', 'source')
    const join = NODE('join', 'join')
    left.data.config = { uri: 'events' }
    right.data.config = { uri: 'images' }
    join.data.config = { how: 'inner', on: '', condition: '' }
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [], nodes: [left, right, join],
        edges: [
          { id: 'left-join', source: 'left', target: 'join', targetHandle: 'a' },
          { id: 'right-join', source: 'right', target: 'join', targetHandle: 'b' },
        ],
      },
      toasts: [],
    })

    useStore.getState().rerunAll()

    expect(apiMocks.estimate).not.toHaveBeenCalled()
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error', msg: 'Choose at least one left and right column.',
    }])
  })

  it('surfaces invalid_graph refusals from user execution but keeps background metadata quiet', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/events.lance' }
    const target = NODE('target', 'filter')
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, target],
      edges: [{ id: 'source-target', source: 'source', target: 'target', data: { wire: 'dataset' as const } }],
    }
    const refusal = new KernelError(400, 'graph has a cycle — control flow must be encapsulated (§5.7)', 'invalid_graph')
    useStore.setState({ doc })
    apiMocks.estimate.mockRejectedValue(refusal)

    await useStore.getState().requestRun('target')
    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error', msg: 'This branch is not ready to run. Check its connections and required fields.',
    }])

    useStore.setState({ toasts: [] })
    apiMocks.graphSizes.mockRejectedValue(refusal)
    await useStore.getState().refreshSchemas()
    expect(useStore.getState().toasts).toEqual([])
  })

  it('attributes a background invalid-graph refusal to the nodes it names', async () => {
    const refusal = new KernelError(
      400,
      "invalid graph: Join node 'join-1' requires exactly one incoming edge on input 'a'; target node 'write-2' has no input port",
      'invalid_graph',
    )
    apiMocks.schema.mockRejectedValue(refusal)

    await useStore.getState().refreshSchemas()

    expect(useStore.getState().toasts).toEqual([])
    expect(useStore.getState().graphRefusals).toEqual({
      'join-1': 'Connect a left dataset',
      'write-2': 'Connect an input',
    })

    apiMocks.schema.mockResolvedValue({})
    await useStore.getState().refreshSchemas()
    expect(useStore.getState().graphRefusals).toEqual({})
  })

  it('does not carry a graph refusal into another canvas', () => {
    useStore.setState({ graphRefusals: { source: 'Connect an input' } })

    useStore.getState().loadDoc({
      id: 'other', version: 1, name: 'other', requirements: [], nodes: [], edges: [],
    }, 'owner', { recoverServerState: false })

    expect(useStore.getState().graphRefusals).toEqual({})
  })

  it.each([
    ['a', 'left'],
    ['b', 'right'],
  ] as const)('translates a pure missing Join input %s into the %s dataset role', async (port, role) => {
    const refusal = new KernelError(
      400,
      `invalid graph: Join node 'join-1' requires exactly one incoming edge on input '${port}'`,
      'invalid_graph',
    )
    apiMocks.estimate.mockRejectedValue(refusal)

    await useStore.getState().requestRun('target')

    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error', msg: `This Join needs a ${role} dataset before it can run.`,
    }])
  })

  it('describes both missing datasets for a pure bare Join refusal', async () => {
    const refusal = new KernelError(
      400,
      "invalid graph: Join node 'join-1' requires exactly one incoming edge on input 'a'; Join node 'join-1' requires exactly one incoming edge on input 'b'",
      'invalid_graph',
    )
    apiMocks.estimate.mockRejectedValue(refusal)

    await useStore.getState().requestRun('target')

    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error', msg: 'This Join needs a left dataset and a right dataset before it can run.',
    }])
  })

  it('hides an unassigned edge id while explaining both missing Join inputs', async () => {
    const refusal = new KernelError(
      400,
      "invalid graph: edge 'e-2-7092' must identify Join input 'a' or 'b' on node 'join-1-7091'; Join node 'join-1-7091' requires exactly one incoming edge on input 'a'; Join node 'join-1-7091' requires exactly one incoming edge on input 'b'",
      'invalid_graph',
    )
    apiMocks.estimate.mockRejectedValue(refusal)

    await useStore.getState().requestRun('target')

    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error', msg: 'This Join needs a left dataset and a right dataset before it can run.',
    }])
  })

  it('keeps an aggregated structural refusal out of the primary toast', async () => {
    const message = "invalid graph: edge 'e' references missing source node 'gone'; Join node 'j' requires exactly one incoming edge on input 'b'"
    const refusal = new KernelError(400, message, 'invalid_graph')
    apiMocks.estimate.mockRejectedValue(refusal)

    await useStore.getState().requestRun('target')

    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error', msg: 'This branch is not ready to run. Check its connections and required fields.',
    }])
  })

  it('does not reinterpret a CustomJoin refusal as the built-in Join contract', async () => {
    const message = "invalid graph: CustomJoin node 'custom-1' requires exactly one incoming edge on input 'b'"
    const refusal = new KernelError(400, message, 'invalid_graph')
    apiMocks.estimate.mockRejectedValue(refusal)

    await useStore.getState().requestRun('target')

    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error', msg: 'This branch is not ready to run. Check its connections and required fields.',
    }])
  })

  it('keeps preview inputs for full runs and refreshes moved heads only after acceptance', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/events.lance' }
    source.data.status = 'latest'
    const target = NODE('target', 'filter')
    target.data.config = { predicate: 'value > 0' }
    target.data.status = 'latest'
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, target],
      edges: [{ id: 'source-target', source: 'source', target: 'target', data: { wire: 'dataset' as const } }],
    }
    const oldManifest = [{
      node_id: 'source', dataset_id: 'dataset', revision_id: '1', provider: 'lance', resolved_at: 'before',
    }]
    const latestManifest = [{ ...oldManifest[0], revision_id: '2', resolved_at: 'after' }]
    useStore.setState({ doc, previews: {}, previewBindings: {}, runs: {} })
    apiMocks.preview.mockResolvedValueOnce({ ...previewResult('old'), inputManifest: oldManifest })

    await useStore.getState().runPreview('target')
    apiMocks.inputDrift.mockResolvedValueOnce({
      drifted: true,
      sources: [{
        nodeId: 'source', datasetId: 'dataset', previewRevisionId: '1', latestRevisionId: '2',
        oldRevisionReadable: true,
        compatibility: { status: 'unknown', fields: [{ kind: 'added', status: 'unknown', newName: 'extra', reason: 'nullability unknown' }] },
      }],
    })

    await useStore.getState().requestRun('target')
    expect(apiMocks.estimate).toHaveBeenCalledWith(doc, 'target', oldManifest)
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(useStore.getState().runs.target).toMatchObject({ phase: 'drift' })
    expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual(['latest', 'latest'])

    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'run-store-test', status: 'done', jobType: 'run', targetNodeId: 'target',
      rowsProcessed: 1, totalRows: 1, ms: 1, placement: 'local', perNode: [],
      outputs: [{ nodeId: 'target', portId: 'out', wire: 'dataset', publicationKind: 'result', outcome: 'committed', uri: '/result.parquet', rows: 1 }],
    })
    await useStore.getState().run('target', false, true)
    expect(apiMocks.run).toHaveBeenCalledWith(
      doc, 'target', false, expect.any(String), oldManifest,
    )
    await vi.waitFor(() => expect(useStore.getState().runs.target.phase).toBe('done'))

    apiMocks.preview.mockResolvedValueOnce({ ...previewResult('latest'), inputManifest: latestManifest })
    await useStore.getState().refreshPreviewInputs('target')
    expect(apiMocks.preview).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: doc.id }), 'target', 50, 0, undefined,
    )
    expect(useStore.getState().previewBindings.target.inputManifest).toEqual(latestManifest)
    expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual(['stale', 'stale'])

    useStore.getState().loadDoc(doc, 'owner')
    expect(useStore.getState().previewBindings.target.inputManifest).toEqual(latestManifest)
  })

  it('recovers a durable full profile against the retained preview manifest after reload', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/events.lance', datasetRef: { parameterRef: 'input' } }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source], edges: [],
      parameters: [{ name: 'input', type: 'dataset' as const, required: true }],
    }
    const manifest = [{
      node_id: 'source', dataset_id: 'dataset', revision_id: '1', provider: 'lance',
      resolved_at: 'before',
    }]
    const digest = 'b'.repeat(64)
    useStore.setState({ doc, previews: {}, previewBindings: {}, profileJobs: {} })
    apiMocks.preview.mockResolvedValueOnce({ ...previewResult('old'), inputManifest: manifest })
    await useStore.getState().runPreview('source')

    apiMocks.profileIdentity.mockResolvedValueOnce({
      targetPortId: 'out', planDigest: digest, inputManifest: manifest,
    })
    apiMocks.executionManifest.mockResolvedValueOnce({
      availability: 'available', document: { parameters: [{
        name: 'input', type: 'dataset',
        value: { kind: 'latest', datasetId: 'dataset', resolvedRevisionId: '1' },
      }] },
    })
    apiMocks.profileJobs.mockResolvedValueOnce([{
      runId: 'profile-recovered-manifest', status: 'done', jobType: 'profile',
      targetNodeId: 'source', targetPortId: 'out', planDigest: digest,
      profileAttemptOrder: 1, rowsProcessed: 0, ms: 10, placement: 'local', perNode: [],
      outputs: [], profile: {
        targetPortId: 'out', columns: [], rowCount: 1, sampled: false,
        completeness: 'complete', notPreviewable: false, inputManifest: manifest,
      },
      executionManifestSha256: 'c'.repeat(64),
    }])

    useStore.getState().loadDoc(doc, 'owner')

    await vi.waitFor(() => expect(apiMocks.profileIdentity).toHaveBeenCalledWith(
      doc, 'source', 'out', manifest,
      [{ name: 'input', value: { kind: 'latest', datasetId: 'dataset' } }],
    ))
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'done', identityVerified: true, inputManifest: manifest,
      parameterBindings: [{ name: 'input', value: { kind: 'latest', datasetId: 'dataset' } }],
      status: { runId: 'profile-recovered-manifest', profile: { inputManifest: manifest } },
    }))
    expect(useStore.getState().previewBindings.source.inputManifest).toEqual(manifest)
  })

  it('invalidates downstream state when refresh replaces the dataset at the same revision id', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/events.lance' }
    source.data.status = 'latest'
    const target = NODE('target', 'filter')
    target.data.config = { predicate: 'value > 0' }
    target.data.status = 'latest'
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, target],
      edges: [{ id: 'source-target', source: 'source', target: 'target', data: { wire: 'dataset' as const } }],
    }
    const oldManifest = [{
      node_id: 'source', dataset_id: 'dataset-a', revision_id: '1', provider: 'lance', resolved_at: 'before',
    }]
    const replacementManifest = [{
      ...oldManifest[0], dataset_id: 'dataset-b', resolved_at: 'after',
    }]
    useStore.setState({ doc, previews: {}, previewBindings: {}, runs: {
      target: { phase: 'drift', inputDrift: { drifted: true, sources: [] }, driftInputManifest: oldManifest },
    } })
    apiMocks.preview
      .mockResolvedValueOnce({ ...previewResult('old'), inputManifest: oldManifest })
      .mockResolvedValueOnce({ ...previewResult('replacement'), inputManifest: replacementManifest })

    await useStore.getState().runPreview('target')
    await useStore.getState().refreshPreviewInputs('target')

    expect(useStore.getState().previewBindings.target.inputManifest).toEqual(replacementManifest)
    expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual(['stale', 'stale'])
    expect(useStore.getState().runs.target).toMatchObject({ phase: 'idle', estimate: undefined })
  })

  it('drops an unavailable retained binding after an explicit successful unversioned refresh', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/events.lance' }
    source.data.status = 'latest'
    const target = NODE('target', 'filter')
    target.data.config = { predicate: 'value > 0' }
    target.data.status = 'latest'
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, target],
      edges: [{ id: 'source-target', source: 'source', target: 'target', data: { wire: 'dataset' as const } }],
    }
    const retainedManifest = [{
      node_id: 'source', dataset_id: 'removed-dataset', revision_id: '1', provider: 'lance', resolved_at: 'before',
    }]
    useStore.setState({ doc, previews: {}, previewBindings: {}, runs: {} })
    apiMocks.preview
      .mockResolvedValueOnce({ ...previewResult('old'), inputManifest: retainedManifest })
      .mockResolvedValueOnce(previewResult('unversioned latest'))

    await useStore.getState().runPreview('target')
    await useStore.getState().refreshPreviewInputs('target')

    expect(useStore.getState().previewBindings.target).toBeUndefined()
    expect(useStore.getState().doc.nodes.map((node) => node.data.status)).toEqual(['stale', 'stale'])
  })

  it('does not accept a drift decision after the preview binding became stale', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/events.lance' }
    const target = NODE('target', 'filter')
    target.data.config = { predicate: 'value > 0' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, target],
      edges: [{ id: 'source-target', source: 'source', target: 'target', data: { wire: 'dataset' as const } }],
    }
    const manifest = [{
      node_id: 'source', dataset_id: 'dataset', revision_id: '1', provider: 'lance', resolved_at: 'before',
    }]
    useStore.setState({ doc, previews: {}, previewBindings: {}, runs: {} })
    apiMocks.preview.mockResolvedValueOnce({ ...previewResult('old'), inputManifest: manifest })
    apiMocks.inputDrift.mockResolvedValueOnce({
      drifted: true,
      sources: [{
        nodeId: 'source', datasetId: 'dataset', previewRevisionId: '1', latestRevisionId: '2',
        oldRevisionReadable: true,
      }],
    })

    await useStore.getState().runPreview('target')
    await useStore.getState().requestRun('target')
    useStore.getState().updateConfig('source', { uri: '/data/other.lance' })
    await useStore.getState().run('target', false, true)

    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(useStore.getState().runs.target).toMatchObject({
      phase: 'failed', error: 'Preview inputs changed; preview again before running.',
    })
  })

  it('records named output count instead of rowsProcessed as result cardinality', async () => {
    const target = NODE('target')
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [target], edges: [] }
    const running = {
      runId: 'named-output-count', status: 'running', jobType: 'run', targetNodeId: 'target',
      rowsProcessed: 50, totalRows: null, ms: 10, placement: 'local', perNode: [], outputs: [],
    }
    apiMocks.run.mockResolvedValueOnce(running)
    apiMocks.runStatus.mockResolvedValueOnce({
      ...running, status: 'done', rowsProcessed: 999, ms: 250,
      outputs: [
        {
          nodeId: 'target', portId: 'pass', wire: 'dataset', publicationKind: 'result',
          outcome: 'committed', uri: '/outputs/pass.parquet', rows: 700,
        },
        {
          nodeId: 'target', portId: 'out', wire: 'dataset', publicationKind: 'result',
          outcome: 'committed', uri: '/outputs/out.parquet', rows: 299,
        },
      ],
    })
    useStore.setState({ doc })

    await useStore.getState().run('target')
    await vi.waitFor(() => expect(useStore.getState().doc.nodes[0].data.status).toBe('latest'))

    const data = useStore.getState().doc.nodes[0].data
    expect(data.lastRun).toEqual({ outputCount: 2, ms: 250, placement: 'local' })
    expect(data.lastRun?.rows).toBeUndefined()
    expect(data.history).toHaveLength(1)
    expect(data.history?.[0]).toMatchObject({ outputCount: 2, label: 'run · 2 outputs' })
    expect(data.currentOutputVersionId).toBe(data.history?.[0].id)
    expect(data.history?.[0].rows).toBeUndefined()
    expect(data.history?.[0].label).not.toContain('999')
  })

  it('uses one deterministic target execution identity for previews and profiles', () => {
    const source = NODE('source')
    source.data.config = { uri: 'events.parquet', options: { batchSize: 10, columns: ['event'] } }
    const target = NODE('target', 'filter')
    target.data.config = { predicate: 'event = view' }
    const otherSource = NODE('other-source')
    otherSource.data.config = { uri: 'other.parquet' }
    const otherTarget = NODE('other-target', 'filter')
    otherTarget.data.config = { predicate: 'score > 0' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: ['pyarrow==20', 'numpy==2'],
      nodes: [otherTarget, source, target, otherSource],
      edges: [
        { id: 'other-edge', source: 'other-source', target: 'other-target', data: { wire: 'dataset' as const } },
        { id: 'target-edge', source: 'source', target: 'target', data: { wire: 'dataset' as const } },
      ],
    }
    const identity = previewPlanIdentity(doc, 'target')
    expect(profilePlanIdentity(doc, 'target')).toBe(identity)

    const reordered = structuredClone(doc)
    reordered.nodes.reverse()
    reordered.edges.reverse()
    reordered.requirements.reverse()
    expect(previewPlanIdentity(reordered, 'target')).toBe(identity)
    expect(profilePlanIdentity(reordered, 'target')).toBe(identity)

    const unrelated = structuredClone(doc)
    unrelated.nodes.find((node) => node.id === 'other-source')!.data.config.uri = 'other-v2.parquet'
    unrelated.edges.find((edge) => edge.id === 'other-edge')!.targetHandle = 'replacement-input'
    expect(previewPlanIdentity(unrelated, 'target')).toBe(identity)
    expect(profilePlanIdentity(unrelated, 'target')).toBe(identity)

    const visualOnly = structuredClone(doc)
    visualOnly.version = 99
    visualOnly.nodes.find((node) => node.id === 'source')!.position = { x: 900, y: 400 }
    visualOnly.nodes.find((node) => node.id === 'target')!.data.status = 'running'
    visualOnly.edges.find((edge) => edge.id === 'target-edge')!.id = 'layout-only-edge-id'
    expect(previewPlanIdentity(visualOnly, 'target')).toBe(identity)
    expect(profilePlanIdentity(visualOnly, 'target')).toBe(identity)

    const upstreamEdit = structuredClone(doc)
    upstreamEdit.nodes.find((node) => node.id === 'source')!.data.config.uri = 'events-v2.parquet'
    expect(previewPlanIdentity(upstreamEdit, 'target')).not.toBe(identity)
    expect(profilePlanIdentity(upstreamEdit, 'target')).not.toBe(identity)

    const targetEdit = structuredClone(doc)
    targetEdit.nodes.find((node) => node.id === 'target')!.data.config.predicate = 'event = purchase'
    expect(previewPlanIdentity(targetEdit, 'target')).not.toBe(identity)
    expect(profilePlanIdentity(targetEdit, 'target')).not.toBe(identity)

    const executionEdgeEdit = structuredClone(doc)
    executionEdgeEdit.edges.find((edge) => edge.id === 'target-edge')!.sourceHandle = 'filtered'
    expect(previewPlanIdentity(executionEdgeEdit, 'target')).not.toBe(identity)
    expect(profilePlanIdentity(executionEdgeEdit, 'target')).not.toBe(identity)

    const metric = NODE('metric', 'metric')
    metric.data.title = 'Revenue'
    const metricDoc = { ...doc, nodes: [source, metric], edges: [
      { id: 'source-metric', source: 'source', target: 'metric', data: { wire: 'dataset' as const } },
    ] }
    const metricRename = structuredClone(metricDoc)
    metricRename.nodes.find((node) => node.id === 'metric')!.data.title = 'Average revenue'
    expect(previewPlanIdentity(metricRename, 'metric')).not.toBe(previewPlanIdentity(metricDoc, 'metric'))
  })

  it('does not treat a legacy Transform scope label as execution semantics', () => {
    const source = NODE('source')
    source.data.config = { uri: 'events.parquet' }
    const transform = NODE('transform', 'transform')
    transform.data.config = { source: 'adhoc', mode: 'map', code: 'def fn(row): return row' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, transform],
      edges: [{ id: 'edge', source: 'source', target: 'transform', data: { wire: 'dataset' as const } }],
    }
    const legacyScope = structuredClone(doc)
    ;(legacyScope.nodes[1].data.config as Record<string, unknown>).scope = 'sample'

    expect(previewPlanIdentity(legacyScope, 'transform')).toBe(previewPlanIdentity(doc, 'transform'))
    expect(profilePlanIdentity(legacyScope, 'transform')).toBe(profilePlanIdentity(doc, 'transform'))
  })

  it('keeps an in-flight profile attached across an unrelated branch edit', async () => {
    const source = NODE('source')
    source.data.config = { uri: 'events.parquet' }
    const target = NODE('target', 'filter')
    target.data.config = { predicate: 'event = view' }
    const otherSource = NODE('other-source')
    otherSource.data.config = { uri: 'other.parquet' }
    const otherTarget = NODE('other-target', 'filter')
    otherTarget.data.config = { predicate: 'score > 0' }
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [],
        nodes: [source, target, otherSource, otherTarget],
        edges: [
          { id: 'source-target', source: 'source', target: 'target', data: { wire: 'dataset' } },
          { id: 'other-branch', source: 'other-source', target: 'other-target', data: { wire: 'dataset' } },
        ],
      },
    })
    await useStore.getState().prepareFullProfile('target')
    const requestGeneration = useStore.getState().profileJobs.target.requestGeneration
    let finish!: (status: any) => void
    apiMocks.fullProfile.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))

    const pending = useStore.getState().startFullProfile('target')
    useStore.getState().updateConfig('other-target', { predicate: 'score > 10' })
    finish({
      runId: 'profile-unrelated-edit', status: 'running', jobType: 'profile', targetNodeId: 'target',
      targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 1, ms: 10, placement: 'local', perNode: [],
    })
    await pending

    expect(useStore.getState().profileJobs.target).toMatchObject({
      requestGeneration, phase: 'running',
      status: { runId: 'profile-unrelated-edit', status: 'running' },
    })
    expect(apiMocks.cancelRun).not.toHaveBeenCalledWith('profile-unrelated-edit')
  })

  it('cancels a full-profile response that arrives for an old graph revision', async () => {
    let resolveJob!: (status: any) => void
    let submitted!: () => void
    const submittedJob = new Promise<void>((resolve) => { submitted = resolve })
    apiMocks.fullProfile.mockImplementationOnce(() => {
      submitted()
      return new Promise((resolve) => { resolveJob = resolve })
    })
    useStore.setState({
      doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] },
    })

    await useStore.getState().prepareFullProfile('source')
    const pending = useStore.getState().startFullProfile('source')
    await submittedJob
    useStore.getState().updateConfig('source', { uri: 'new-events.parquet' })
    resolveJob({ runId: 'profile-old', status: 'queued', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [] })
    await pending

    expect(apiMocks.cancelRun).toHaveBeenCalledWith('profile-old')
    expect(useStore.getState().profileJobs.source?.status).toBeUndefined()
  })

  it('cancels a profile when a metric title changes while submission is in flight', async () => {
    let finish!: (status: any) => void
    apiMocks.fullProfile.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const metric = NODE('metric', 'metric')
    metric.data.title = 'Revenue'
    useStore.setState({
      doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [metric], edges: [] },
    })
    await useStore.getState().prepareFullProfile('metric')
    const pending = useStore.getState().startFullProfile('metric')
    useStore.getState().updateData('metric', { title: 'Average revenue' })
    finish({
      runId: 'profile-old-metric-title', status: 'queued', jobType: 'profile', targetNodeId: 'metric',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })
    await pending

    expect(apiMocks.cancelRun).toHaveBeenCalledWith('profile-old-metric-title')
    expect(useStore.getState().profileJobs.metric?.status).toBeUndefined()
  })

  it('cancels a section profile when a nested descendant alias title changes in flight', async () => {
    let finish!: (status: any) => void
    apiMocks.fullProfile.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const section = NODE('section', 'section')
    const nested = { ...NODE('nested', 'section'), parentId: 'section' }
    const child = { ...NODE('child', 'filter'), parentId: 'nested' }
    child.data.title = 'Clean rows'
    useStore.setState({
      doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [section, nested, child], edges: [] },
    })
    await useStore.getState().prepareFullProfile('section')
    const pending = useStore.getState().startFullProfile('section')
    useStore.getState().updateData('child', { title: 'Keep valid rows' })
    finish({
      runId: 'profile-old-section-alias', status: 'queued', jobType: 'profile', targetNodeId: 'section',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })
    await pending

    expect(apiMocks.cancelRun).toHaveBeenCalledWith('profile-old-section-alias')
    expect(useStore.getState().profileJobs.section?.status).toBeUndefined()
  })

  it('keeps a whole-dataset profile behind visible preflight and an explicit start', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const manifest = [{
      node_id: 'source', dataset_id: 'dataset', revision_id: '1', provider: 'lance',
      resolved_at: 'before',
    }]
    useStore.setState({ doc, previewBindings: { source: {
      canvasId: doc.id, nodeId: 'source', planIdentity: previewPlanIdentity(doc, 'source'),
      inputManifest: manifest,
    } } })
    apiMocks.profileEstimate.mockResolvedValueOnce({
      rows: null, bytes: null, placement: 'local', needsConfirm: true,
      targetPortId: 'out', planDigest: 'a'.repeat(64), inputManifest: manifest,
    })

    await useStore.getState().prepareFullProfile('source')

    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'preflight', estimate: { needsConfirm: true }, inputManifest: manifest,
    })
    expect(apiMocks.profileEstimate).toHaveBeenCalledWith(doc, 'source', 'out', manifest)
    expect(apiMocks.fullProfile).not.toHaveBeenCalled()

    apiMocks.fullProfile.mockResolvedValueOnce({
      runId: 'profile-confirmed', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })
    await useStore.getState().startFullProfile('source')

    expect(apiMocks.fullProfile).toHaveBeenCalledWith(
      doc, 'source', 'out', useStore.getState().profileJobs.source.planDigest,
      expect.any(String), true, manifest,
    )
  })

  it('keeps sibling named-output profile identities and results separate', async () => {
    const section = NODE('branches', 'section')
    section.data.config = { outputs: ['left', 'right'] }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [section], edges: [],
    }
    useStore.setState({ doc })
    apiMocks.profileEstimate.mockImplementation(async (_doc, _nodeId, portId) => ({
      rows: 1, bytes: 10, placement: 'local', needsConfirm: false,
      targetPortId: portId, planDigest: portId === 'left' ? '1'.repeat(64) : '2'.repeat(64),
    }))
    apiMocks.fullProfile.mockImplementation(async (_doc, _nodeId, portId, planDigest) => ({
      runId: `profile-${portId}`, status: 'done', jobType: 'profile',
      targetNodeId: 'branches', targetPortId: portId, planDigest, profileAttemptOrder: 1,
      rowsProcessed: 1, ms: 1, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 1, sampled: false, targetPortId: portId },
    }))

    await useStore.getState().prepareFullProfile('branches', 'left')
    await useStore.getState().prepareFullProfile('branches', 'right')
    await useStore.getState().startFullProfile('branches', 'left')
    await useStore.getState().startFullProfile('branches', 'right')

    const jobs = useStore.getState().profileJobs
    expect(jobs[profileJobKey('branches', 'left')].status).toMatchObject({
      runId: 'profile-left', targetPortId: 'left', planDigest: '1'.repeat(64),
    })
    expect(jobs[profileJobKey('branches', 'right')].status).toMatchObject({
      runId: 'profile-right', targetPortId: 'right', planDigest: '2'.repeat(64),
    })
  })

  it('surfaces unsupported destination credentials from run preflight without starting', async () => {
    const write = NODE('write', 'write')
    write.data.config = { destId: 'exports', filename: 'out.parquet' }
    useStore.setState({
      doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [write], edges: [] },
    })
    const message = "Execution backend 'local-subprocess' cannot use the destination-specific credential selected for destination 'Research exports'. Select 'local-out-of-core' for in-process credential resolution, or clear the destination/default credential to use ambient workload identity. No run was started."
    apiMocks.estimate.mockRejectedValueOnce(new KernelError(400, message))

    await useStore.getState().requestRun('write')

    expect(useStore.getState().runs.write).toMatchObject({ phase: 'failed', error: message })
    expect(useStore.getState().toasts.some((toast) => toast.msg === message && toast.kind === 'error')).toBe(true)
  })

  it('routes configured column-merge Writes to their certified panel instead of the ordinary runner', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/source.parquet' }
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', mergeColumns: {
      identityColumns: ['id'], rules: [{ source: 'score', target: 'score', mode: 'add' }],
    } }
    useStore.setState({ doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }] } })

    await useStore.getState().requestRun('write')
    await useStore.getState().run('write')

    expect(apiMocks.estimate).not.toHaveBeenCalled()
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(apiMocks.writeAdmission).not.toHaveBeenCalled()
    expect(useStore.getState().openPanels).toEqual({ write: 'run' })
    expect(useStore.getState().toasts.some((toast) => toast.msg === 'Review the column merge setup before running.')).toBe(true)
  })

  it('routes even an empty managed-sidecar merge draft to its certified panel instead of ordinary Write execution', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/source.parquet' }
    const write = NODE('write', 'write')
    // An empty draft is still an explicit choice to use the certified sidecar flow. It must not
    // quietly fall through to ordinary Write admission while the researcher completes it.
    write.data.config = { filename: 'output.parquet', managedSidecarMerge: {} }
    useStore.setState({ doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }] } })

    await useStore.getState().requestRun('write')
    await useStore.getState().run('write')

    expect(apiMocks.estimate).not.toHaveBeenCalled()
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(apiMocks.writeAdmission).not.toHaveBeenCalled()
    expect(useStore.getState().openPanels).toEqual({ write: 'run' })
    expect(useStore.getState().toasts.some((toast) => toast.msg === 'Review the saved-dataset column merge before running.')).toBe(true)
  })

  it('fails closed to the managed-sidecar panel when an imported draft is malformed', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/source.parquet' }
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', managedSidecarMerge: ['corrupt-draft'] }
    useStore.setState({ doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }] } })

    await useStore.getState().requestRun('write')

    expect(apiMocks.estimate).not.toHaveBeenCalled()
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(apiMocks.writeAdmission).not.toHaveBeenCalled()
    expect(useStore.getState().openPanels).toEqual({ write: 'run' })
  })

  it('routes configured keyed-upsert Writes to their certified panel instead of the ordinary runner', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/source.parquet' }
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', keyedUpsert: { keys: ['id'] } }
    useStore.setState({ doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }] } })

    await useStore.getState().requestRun('write')
    await useStore.getState().run('write')

    expect(apiMocks.estimate).not.toHaveBeenCalled()
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(apiMocks.writeAdmission).not.toHaveBeenCalled()
    expect(useStore.getState().openPanels).toEqual({ write: 'run' })
    expect(useStore.getState().toasts.some((toast) => toast.msg === 'Review the upsert setup before running.')).toBe(true)
  })

  it('runs the exact managed-local intent shown by write admission', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/source.parquet' }
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'edge', source: 'source', target: 'write' }] }
    useStore.setState({ doc })
    const intent = {
      destination: { logicalUri: '/outputs/output.parquet', name: 'output', provider: 'managed-local-file' as const },
      mode: 'create' as const, expectedSchema: [{ name: 'value', type: 'int' }], expectedHead: null,
      idempotencyKey: 'write-key', partitions: [], provenance: { publication: {
        idempotencyKey: 'write-key', runId: 'run-write', producer: 'c', producerVersion: 1,
        stepId: 'write', provenance: 'run',
      }, parents: ['/data/source.parquet'] },
    }
    apiMocks.writeAdmission.mockResolvedValueOnce({
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet', mode: 'create',
      provider: 'managed-local-file', expectedSchema: intent.expectedSchema, partitions: [], intent,
    })
    apiMocks.run.mockResolvedValueOnce({
      runId: 'run-write', status: 'running', jobType: 'run', targetNodeId: 'write', rowsProcessed: 0,
      ms: 0, placement: 'local', perNode: [], outputs: [],
    })
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'run-write', status: 'done', jobType: 'run', targetNodeId: 'write', rowsProcessed: 2,
      totalRows: 2, ms: 5, placement: 'local', perNode: [], outputs: [{
        nodeId: 'write', portId: 'out', wire: 'dataset', publicationKind: 'catalog',
        outcome: 'committed', uri: '/artifacts/rev.parquet', table: 'output', version: 'rev-1', rows: 2,
      }],
    })

    await useStore.getState().run('write')

    const submissionId = apiMocks.writeAdmission.mock.calls[0][2]
    expect(apiMocks.run).toHaveBeenCalledWith(
      doc, 'write', false, submissionId, undefined, intent, undefined, undefined)
    await vi.waitFor(() => expect(useStore.getState().runs.write.phase).toBe('done'))
    expect(useStore.getState().runs.write.writeAdmission).toBeUndefined()
    expect(useStore.getState().runs.write.writeOutcomeAdmission).toMatchObject({
      mode: 'create', intent: { idempotencyKey: 'write-key' },
    })

    apiMocks.writeAdmission.mockResolvedValueOnce({
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet', mode: 'replace',
      provider: 'managed-local-file', expectedSchema: intent.expectedSchema, partitions: [],
    })
    await useStore.getState().prepareWrite('write')
    expect(useStore.getState().runs.write.writeOutcomeAdmission).toMatchObject({ mode: 'create' })
    expect(useStore.getState().runs.write.writeAdmission).toMatchObject({ mode: 'replace' })
  })

  it('persists the exact managed receipt run and keeps it until another successful Write replaces it', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/source.parquet' }
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
        edges: [{ id: 'source-write', source: 'source', target: 'write' }],
      },
      runs: {},
    })
    const admission = {
      nodeId: 'write', managed: true, destination: 'managed://dataset-1', mode: 'create' as const,
      provider: 'managed-local-file', expectedSchema: [], partitions: [],
    }
    apiMocks.writeAdmission.mockResolvedValueOnce(admission)
    apiMocks.run.mockResolvedValueOnce({
      runId: 'published-run', status: 'running', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [], outputs: [],
    })
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'published-run', status: 'done', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 2, totalRows: 2, ms: 5, placement: 'local', perNode: [],
      outputs: [WRITE_OUTPUT('revision-1')],
    })

    await useStore.getState().run('write')
    await vi.waitFor(() => expect(useStore.getState().runs.write.phase).toBe('done'))

    expect(useStore.getState().doc.nodes.find((node) => node.id === 'write')?.data.lastRun)
      .toMatchObject({ writeReceiptRunId: 'published-run', rows: 2 })
    expect(useStore.getState().runs.write.writeOutcome).toMatchObject({
      runId: 'published-run', receipt: { datasetId: 'dataset-1', revisionId: 'revision-1' },
    })

    apiMocks.writeAdmission.mockResolvedValueOnce({ ...admission, mode: 'replace' })
    apiMocks.run.mockResolvedValueOnce({
      runId: 'failed-run', status: 'running', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [], outputs: [],
    })
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'failed-run', status: 'failed', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 1, ms: 3, placement: 'local', perNode: [], outputs: [], error: 'write failed',
    })

    await useStore.getState().run('write')
    await vi.waitFor(() => expect(useStore.getState().runs.write.phase).toBe('failed'))
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'write')?.data.lastRun)
      .toMatchObject({ writeReceiptRunId: 'published-run' })
    expect(useStore.getState().runs.write.writeOutcome?.runId).toBe('published-run')

    apiMocks.writeAdmission.mockResolvedValueOnce({
      ...admission, managed: false, destination: '/tmp/output.parquet',
      provider: 'local-file', mode: 'overwrite',
    })
    apiMocks.run.mockResolvedValueOnce({
      runId: 'provider-run', status: 'running', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [], outputs: [],
    })
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'provider-run', status: 'done', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 2, totalRows: 2, ms: 4, placement: 'local', perNode: [], outputs: [{
        nodeId: 'write', portId: 'out', wire: 'dataset', publicationKind: 'result',
        outcome: 'committed', rows: 2,
      }],
    })

    await useStore.getState().run('write')
    await vi.waitFor(() => expect(useStore.getState().runs.write).toMatchObject({
      phase: 'done', status: { runId: 'provider-run', status: 'done' },
    }))
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'write')?.data.lastRun)
      .not.toHaveProperty('writeReceiptRunId')
    expect(useStore.getState().runs.write.writeOutcome).toBeUndefined()
  })

  it('recovers one exact durable Write receipt without restoring its admission identity', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/source.parquet' }
    const write = NODE('write', 'write')
    write.data.status = 'latest'
    ;(write.data as any).lastRun = {
      rows: 2, ms: 5, placement: 'local', writeReceiptRunId: 'published-run',
    }
    const doc = {
      id: 'c', version: 2, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    apiMocks.workspaceJobs.mockResolvedValueOnce({
      items: [WRITE_JOB('c', 'published-run')], hasMore: false,
    })

    useStore.getState().loadDoc(doc, 'owner')

    await vi.waitFor(() => expect(useStore.getState().runs.write?.writeOutcome?.runId)
      .toBe('published-run'))
    expect(apiMocks.workspaceJobs).toHaveBeenCalledWith({
      limit: 2, status: 'done', canvasId: 'c', nodeId: 'write', runId: 'published-run',
    })
    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'idle',
      writeOutcome: {
        runId: 'published-run',
        receipt: { datasetId: 'dataset-1', revisionId: 'revision-1' },
      },
    })
    expect(useStore.getState().runs.write.writeAdmission).toBeUndefined()
    expect(useStore.getState().runs.write.writeOutcomeAdmission).toBeUndefined()
    expect(useStore.getState().runs.write.writeSubmissionId).toBeUndefined()

    useStore.getState().updateConfig('source', { uri: '/data/replaced.parquet' })
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'write')?.data.status)
      .toBe('stale')
    expect(useStore.getState().runs.write.writeOutcome?.runId).toBe('published-run')
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'write')
      ?.data.lastRun?.writeReceiptRunId).toBe('published-run')
  })

  it('fails closed on ambiguous or mismatched exact Write receipt projections', async () => {
    const cases = [
      [],
      [WRITE_JOB('c', 'published-run'), WRITE_JOB('c', 'published-run')],
      [{ ...WRITE_JOB('other-canvas', 'published-run') }],
      [{ ...WRITE_JOB('c', 'other-run') }],
      [{ ...WRITE_JOB('c', 'published-run'), targetNodeId: 'other-node' }],
      [{ ...WRITE_JOB('c', 'published-run'), outputReceipt: WRITE_RECEIPT('other-revision') }],
    ]
    for (const items of cases) {
      const write = NODE('write', 'write')
      write.data.status = 'latest'
      ;(write.data as any).lastRun = {
        rows: 2, ms: 5, placement: 'local', writeReceiptRunId: 'published-run',
      }
      apiMocks.workspaceJobs.mockResolvedValueOnce({ items, hasMore: false })
      useStore.getState().loadDoc({
        id: 'c', version: 2, name: 'test', requirements: [], nodes: [write], edges: [],
      }, 'owner')
      await vi.waitFor(() => expect(apiMocks.workspaceJobs).toHaveBeenCalledTimes(
        cases.indexOf(items) + 1,
      ))
      await Promise.resolve()
      expect(useStore.getState().runs.write?.writeOutcome).toBeUndefined()
    }
  })

  it('keeps fresh local Write state and rejects a delayed receipt after navigation', async () => {
    const write = NODE('write', 'write')
    write.data.status = 'latest'
    ;(write.data as any).lastRun = {
      rows: 2, ms: 5, placement: 'local', writeReceiptRunId: 'published-run',
    }
    let finishFirst!: (page: any) => void
    let finishSecond!: (page: any) => void
    apiMocks.workspaceJobs
      .mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishSecond = resolve }))

    useStore.getState().loadDoc({
      id: 'c', version: 2, name: 'test', requirements: [], nodes: [write], edges: [],
    }, 'owner')
    useStore.setState((state) => ({
      runs: { ...state.runs, write: {
        phase: 'running',
        status: {
          runId: 'new-running', status: 'running', jobType: 'run', targetNodeId: 'write',
          rowsProcessed: 0, ms: 0, placement: 'local', perNode: [], outputs: [],
        },
        writeAdmission: {
          nodeId: 'write', managed: true, destination: 'managed://dataset-1', mode: 'replace',
          provider: 'managed-local-file', expectedSchema: [], partitions: [],
        },
        writeSubmissionId: 'new-submission',
      } },
    }))
    finishFirst({ items: [WRITE_JOB('c', 'published-run')], hasMore: false })
    await vi.waitFor(() => expect(useStore.getState().runs.write.writeOutcome?.runId)
      .toBe('published-run'))
    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'running', status: { runId: 'new-running' },
      writeSubmissionId: 'new-submission', writeAdmission: { mode: 'replace' },
    })

    useStore.getState().loadDoc({
      id: 'c', version: 3, name: 'test', requirements: [], nodes: [{
        ...write,
        data: { ...write.data, lastRun: {
          rows: 3, ms: 7, placement: 'local' as const, writeReceiptRunId: 'new-published-run',
        } },
      }], edges: [],
    }, 'owner')
    useStore.getState().loadDoc({
      id: 'other', version: 1, name: 'other', requirements: [], nodes: [], edges: [],
    }, 'owner')
    finishSecond({ items: [WRITE_JOB('c', 'new-published-run', 'revision-2')], hasMore: false })
    await Promise.resolve()
    await Promise.resolve()
    expect(useStore.getState().doc.id).toBe('other')
    expect(useStore.getState().runs.write).toBeUndefined()
  })

  it('rejects a delayed Write receipt after a newer success replaces its same-canvas pointer', async () => {
    const write = NODE('write', 'write')
    write.data.status = 'latest'
    ;(write.data as any).lastRun = {
      rows: 2, ms: 5, placement: 'local', writeReceiptRunId: 'old-published-run',
    }
    let finish!: (page: any) => void
    apiMocks.workspaceJobs.mockImplementationOnce(
      () => new Promise((resolve) => { finish = resolve }),
    )
    useStore.getState().loadDoc({
      id: 'c', version: 2, name: 'test', requirements: [], nodes: [write], edges: [],
    }, 'owner')
    useStore.getState().updateData('write', {
      lastRun: {
        rows: 3, ms: 7, placement: 'local', writeReceiptRunId: 'new-published-run',
      },
    })

    finish({ items: [WRITE_JOB('c', 'old-published-run')], hasMore: false })
    await Promise.resolve()
    await Promise.resolve()

    expect(useStore.getState().doc.nodes[0].data.lastRun?.writeReceiptRunId)
      .toBe('new-published-run')
    expect(useStore.getState().runs.write?.writeOutcome).toBeUndefined()
  })

  it('keeps unchanged replace admission on the automatic fast path', async () => {
    const source = NODE('source')
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    useStore.setState({ doc, runs: {} })
    const intent = {
      destination: {
        logicalUri: '/outputs/output.parquet', name: 'output',
        datasetId: 'dataset-1', provider: 'managed-local-file' as const,
      },
      mode: 'replace' as const, expectedSchema: [{ name: 'value', type: 'int' }],
      expectedHead: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'revision-1' },
      idempotencyKey: 'unchanged-write', partitions: [], provenance: {
        publication: { idempotencyKey: 'unchanged-write', provenance: 'run' }, parents: [],
      },
      schemaDrift: {
        comparedHead: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'revision-1' },
        compatibility: { status: 'unknown' as const, fields: [{
          kind: 'unchanged' as const, status: 'unknown' as const,
          oldName: 'value', newName: 'value',
          reason: 'logical type is unchanged; nullability is not proven on both versions',
        }] },
        requiresConfirmation: false,
      },
    }
    apiMocks.writeAdmission.mockResolvedValueOnce({
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet', mode: 'replace',
      provider: 'managed-local-file', expectedSchema: intent.expectedSchema, partitions: [],
      expectedHead: intent.expectedHead, intent,
    })
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'run-store-test', status: 'done', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 1, totalRows: 1, ms: 1, placement: 'local', perNode: [], outputs: [],
    })

    await useStore.getState().requestRun('write')

    expect(apiMocks.run).toHaveBeenCalledWith(
      doc, 'write', false, expect.any(String), undefined, intent, undefined, undefined)
    expect(useStore.getState().runs.write.phase).not.toBe('confirm')
    await vi.waitFor(() => expect(useStore.getState().runs.write.phase).toBe('done'))
    useStore.setState({ runs: {} })
  })

  it('requires an explicit confirmation before overwriting provider output', async () => {
    const source = NODE('source')
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    useStore.setState({ doc, runs: {} })
    apiMocks.writeAdmission.mockResolvedValueOnce({
      nodeId: 'write', managed: false, destination: 's3://provider/output.parquet',
      mode: 'overwrite', provider: 'provider-sink', expectedSchema: [], partitions: [],
    })

    await useStore.getState().requestRun('write')

    expect(useStore.getState().runs.write).toMatchObject({ phase: 'confirm' })
    expect(apiMocks.run).not.toHaveBeenCalled()
  })

  it('submits a provider overwrite after re-admitting the displayed destination', async () => {
    const source = NODE('source')
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    const admission = {
      nodeId: 'write', managed: false, destination: 's3://provider/output.parquet',
      mode: 'overwrite' as const, provider: 'provider-sink', expectedSchema: [], partitions: [],
    }
    useStore.setState({ doc, runs: {} })
    apiMocks.writeAdmission.mockResolvedValueOnce(admission).mockResolvedValueOnce(admission)
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'provider-overwrite', status: 'done', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 1, totalRows: 1, ms: 1, placement: 'local', perNode: [], outputs: [],
    })

    await useStore.getState().requestRun('write')
    const displayedSubmission = useStore.getState().runs.write.writeSubmissionId
    await useStore.getState().run('write', true)

    expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(2)
    const submittedAdmission = apiMocks.writeAdmission.mock.calls[1][2]
    expect(submittedAdmission).not.toBe(displayedSubmission)
    expect(apiMocks.run).toHaveBeenCalledWith(
      doc, 'write', true, submittedAdmission, undefined, undefined, undefined, undefined)
  })

  it('re-admits a provider overwrite and does not reuse confirmation after destination drift', async () => {
    const source = NODE('source')
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    const shown = {
      nodeId: 'write', managed: false, destination: 's3://provider-a/output.parquet',
      mode: 'overwrite' as const, provider: 'provider-a', expectedSchema: [], partitions: [],
    }
    const fresh = {
      ...shown, destination: 's3://provider-b/output.parquet', provider: 'provider-b',
    }
    useStore.setState({ doc, runs: {} })
    apiMocks.writeAdmission.mockResolvedValueOnce(shown).mockResolvedValueOnce(fresh)

    await useStore.getState().requestRun('write')
    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'confirm', writeAdmission: shown,
    })

    await useStore.getState().run('write', true)

    expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(2)
    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'confirm', writeAdmission: fresh,
    })
    expect(apiMocks.run).not.toHaveBeenCalled()
  })

  it('shows exact drift admission before submitting that same confirmed write', async () => {
    const source = NODE('source')
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    useStore.setState({ doc, runs: {} })
    const comparedHead = {
      kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'revision-1',
    }
    const intent = {
      destination: {
        logicalUri: '/outputs/output.parquet', name: 'output',
        datasetId: 'dataset-1', provider: 'managed-local-file' as const,
      },
      mode: 'replace' as const, expectedSchema: [
        { name: 'value', type: 'int' }, { name: 'extra', type: 'string' },
      ],
      expectedHead: comparedHead, idempotencyKey: 'drift-write', partitions: [],
      provenance: {
        publication: { idempotencyKey: 'drift-write', provenance: 'run' }, parents: [],
      },
      schemaDrift: {
        comparedHead,
        compatibility: { status: 'compatible' as const, fields: [{
          kind: 'added' as const, status: 'compatible' as const,
          newName: 'extra', reason: 'nullable field was added',
        }] },
        requiresConfirmation: true,
      },
    }
    const admission = {
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet',
      mode: 'replace' as const, provider: 'managed-local-file',
      expectedSchema: intent.expectedSchema, partitions: [], expectedHead: comparedHead, intent,
    }
    apiMocks.writeAdmission.mockResolvedValueOnce(admission)
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'run-store-test', status: 'done', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 1, totalRows: 1, ms: 1, placement: 'local', perNode: [], outputs: [],
    })

    await useStore.getState().requestRun('write')

    const submissionId = apiMocks.writeAdmission.mock.calls[0][2]
    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'confirm', writeAdmission: admission, writeSubmissionId: submissionId,
    })
    expect(useStore.getState().openPanels).toEqual({ write: 'run' })
    expect(apiMocks.run).not.toHaveBeenCalled()

    await useStore.getState().run('write', true)

    expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(1)
    expect(apiMocks.run).toHaveBeenCalledWith(
      doc, 'write', true, submissionId, undefined, intent, undefined, intent)
    await vi.waitFor(() => expect(useStore.getState().runs.write.phase).toBe('done'))
    useStore.setState({ runs: {} })
  })

  it('replaces stale schema-drift evidence instead of reusing its confirmation', async () => {
    const source = NODE('source')
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
      edges: [{ id: 'source-write', source: 'source', target: 'write' }],
    }
    useStore.setState({ doc, runs: {} })
    const intentA = {
      destination: {
        logicalUri: '/outputs/output.parquet', name: 'output',
        datasetId: 'dataset-1', provider: 'managed-local-file' as const,
      },
      mode: 'replace' as const, expectedSchema: [{ name: 'id', type: 'int' }],
      expectedHead: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'revision-1' },
      idempotencyKey: 'drift-a', partitions: [], provenance: {
        publication: { idempotencyKey: 'drift-a', provenance: 'run' }, parents: [],
      },
      schemaDrift: {
        comparedHead: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'revision-1' },
        compatibility: { status: 'breaking' as const, fields: [{
          kind: 'removed' as const, status: 'breaking' as const, oldName: 'user_id',
          reason: 'field was removed',
        }] },
        requiresConfirmation: true,
      },
    }
    const intentB = {
      ...intentA,
      expectedSchema: [{ name: 'user_id', type: 'int' }],
      expectedHead: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'revision-2' },
      idempotencyKey: 'drift-b',
      provenance: { publication: { idempotencyKey: 'drift-b', provenance: 'run' }, parents: [] },
      schemaDrift: {
        comparedHead: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'revision-2' },
        compatibility: { status: 'breaking' as const, fields: [{
          kind: 'removed' as const, status: 'breaking' as const, oldName: 'id',
          reason: 'field was removed',
        }] },
        requiresConfirmation: true,
      },
    }
    const admissionA = {
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet', mode: 'replace' as const,
      provider: 'managed-local-file', expectedSchema: intentA.expectedSchema, partitions: [],
      expectedHead: intentA.expectedHead, intent: intentA,
    }
    const admissionB = {
      ...admissionA, expectedSchema: intentB.expectedSchema, expectedHead: intentB.expectedHead, intent: intentB,
    }
    apiMocks.writeAdmission.mockResolvedValueOnce(admissionA).mockResolvedValueOnce(admissionB)

    await useStore.getState().requestRun('write')
    const changedDoc = {
      ...doc,
      nodes: doc.nodes.map((node) => node.id === 'source'
        ? { ...node, data: { ...node.data, config: { ...node.data.config, filter: 'value > 0' } } }
        : node),
    }
    // Simulate the click racing with graph invalidation: the displayed A is still retained when
    // the fresh admission observes B.
    useStore.setState({ doc: changedDoc })

    await useStore.getState().run('write', true)

    expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(2)
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(useStore.getState().runs.write).toMatchObject({ phase: 'confirm', writeAdmission: admissionB })

    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'run-store-test', status: 'done', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 1, totalRows: 1, ms: 1, placement: 'local', perNode: [], outputs: [],
    })
    await useStore.getState().run('write', true)

    expect(apiMocks.run).toHaveBeenCalledWith(
      changedDoc, 'write', true, expect.any(String), undefined, intentB, undefined, intentB)
    await vi.waitFor(() => expect(useStore.getState().runs.write.phase).toBe('done'))
    useStore.setState({ runs: {} })
  })

  it('enters confirmation only for an unconfirmed typed confirmation response', async () => {
    const source = NODE('source')
    const target = NODE('target', 'transform')
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, target],
        edges: [{ id: 'source-target', source: 'source', target: 'target' }],
      },
      runs: {}, toasts: [],
    })
    apiMocks.run.mockRejectedValueOnce(new KernelError(
      409, 'run needs confirmation (large or unknown size — a full pass)', 'run_confirmation_required'))

    await useStore.getState().run('target')

    expect(useStore.getState().runs.target).toMatchObject({ phase: 'confirm' })
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'target')?.data.status).toBe('stale')
    expect(useStore.getState().toasts).toEqual([])

    apiMocks.run.mockRejectedValueOnce(new KernelError(
      409, 'run needs confirmation (large or unknown size — a full pass)', 'run_confirmation_required'))
    await useStore.getState().run('target', true)

    expect(useStore.getState().runs.target).toMatchObject({
      phase: 'failed', error: 'run needs confirmation (large or unknown size — a full pass)',
    })
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'target')?.data.status).toBe('failed')
    expect(useStore.getState().toasts).toMatchObject([{
      kind: 'error', msg: 'run needs confirmation (large or unknown size — a full pass)',
    }])
  })

  it('surfaces a structural conflict instead of returning it to confirmation', async () => {
    const source = NODE('source')
    const filter = NODE('filter', 'filter')
    const transform = NODE('transform', 'transform')
    const detail = 'linear checkpoint tasks require exactly Source -> Select(checkpoint) -> Write'
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, filter, transform],
        edges: [
          { id: 'source-filter', source: 'source', target: 'filter' },
          { id: 'filter-transform', source: 'filter', target: 'transform' },
        ],
      },
      runs: {}, toasts: [],
    })
    apiMocks.run.mockRejectedValueOnce(new KernelError(409, detail, 'conflict'))

    await useStore.getState().run('transform', true)

    expect(useStore.getState().runs.transform).toMatchObject({ phase: 'failed', error: detail })
    expect(useStore.getState().doc.nodes.find((node) => node.id === 'transform')?.data.status).toBe('failed')
    expect(useStore.getState().toasts).toMatchObject([{ kind: 'error', msg: detail }])
  })

  it('surfaces a stale write admission as re-admission work, not a size confirmation', async () => {
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    const source = NODE('source')
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [], nodes: [source, write],
        edges: [{ id: 'source-write', source: 'source', target: 'write' }],
      },
      runs: {},
    })
    const admission = {
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet', mode: 'replace',
      provider: 'managed-local-file', expectedSchema: [], partitions: [], intent: { marker: 'frozen' },
    }
    apiMocks.writeAdmission.mockResolvedValueOnce(admission)
    apiMocks.run.mockRejectedValueOnce(new KernelError(
      409, 'write admission is stale; re-admit the current destination head and retry'))

    await useStore.getState().run('write')

    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'failed',
      error: 'Destination changed before this run started. Review the latest version and try again.',
    })
    expect(useStore.getState().runs.write.writeAdmission).toBeUndefined()
    expect(useStore.getState().toasts).toContainEqual(expect.objectContaining({
      kind: 'error',
      msg: 'Destination changed before this run started. Review the latest version and try again.',
    }))
    expect(useStore.getState().toasts.some(({ msg }) => msg.includes('write admission is stale'))).toBe(false)

    const fresh = {
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet', mode: 'replace',
      provider: 'managed-local-file', expectedSchema: [], partitions: [],
      intent: { marker: 'fresh', schemaDrift: { requiresConfirmation: false } },
    }
    apiMocks.writeAdmission.mockResolvedValueOnce(fresh)
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'run-store-test', status: 'done', jobType: 'run', targetNodeId: 'write',
      rowsProcessed: 1, totalRows: 1, ms: 1, placement: 'local', perNode: [], outputs: [],
    })
    await useStore.getState().requestRun('write')

    expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(2)
    expect(apiMocks.writeAdmission.mock.calls[1][2]).not.toBe(apiMocks.writeAdmission.mock.calls[0][2])
    expect(apiMocks.run.mock.calls[1][5]).toEqual(fresh.intent)
    await vi.waitFor(() => expect(useStore.getState().runs.write.phase).toBe('done'))
    useStore.setState({ runs: {} })
  })

  it('reuses the admitted write submission after response loss and a Canvas version save', async () => {
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.parquet', writeMode: 'overwrite' }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [write], edges: [] }
    useStore.setState({ doc })
    const intent = {
      destination: { logicalUri: '/outputs/output.parquet', name: 'output', provider: 'managed-local-file' as const },
      mode: 'create' as const, expectedSchema: [], expectedHead: null,
      idempotencyKey: 'write-key', partitions: [], provenance: { publication: {
        idempotencyKey: 'write-key', runId: 'run-write', producer: 'c', producerVersion: 1,
        stepId: 'write', provenance: 'run',
      }, parents: [] },
    }
    const admission = {
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet', mode: 'create' as const,
      provider: 'managed-local-file', expectedSchema: [], partitions: [], intent,
    }
    apiMocks.writeAdmission.mockResolvedValueOnce(admission)
    apiMocks.run.mockRejectedValueOnce(new Error('network response lost')).mockResolvedValueOnce({
      runId: 'run-write', status: 'running', jobType: 'run', targetNodeId: 'write', rowsProcessed: 0,
      ms: 0, placement: 'local', perNode: [], outputs: [],
    })
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'run-write', status: 'done', jobType: 'run', targetNodeId: 'write', rowsProcessed: 2,
      totalRows: 2, ms: 5, placement: 'local', perNode: [], outputs: [{
        nodeId: 'write', portId: 'out', wire: 'dataset', publicationKind: 'catalog',
        outcome: 'committed', uri: '/artifacts/rev.parquet', table: 'output', version: 'rev-1', rows: 2,
      }],
    })

    await useStore.getState().run('write')

    const submissionId = apiMocks.writeAdmission.mock.calls[0][2]
    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'failed', writeAdmission: admission, writeSubmissionId: submissionId,
    })
    // Autosave advances Canvas metadata independently of execution semantics. It must not invalidate
    // the admitted write or mint a second submission identity after an ambiguous response loss.
    useStore.setState((state) => ({ doc: { ...state.doc, version: state.doc.version + 1 } }))

    await useStore.getState().run('write')

    expect(apiMocks.writeAdmission).toHaveBeenCalledTimes(1)
    expect(apiMocks.run.mock.calls[1].slice(1)).toEqual([
      'write', false, submissionId, undefined, intent, undefined, undefined,
    ])
    expect(apiMocks.run.mock.calls[1][0].nodes[0].data.config).toEqual(doc.nodes[0].data.config)
    expect(apiMocks.run.mock.calls[1][0].version).toBe(2)
    await vi.waitFor(() => expect(useStore.getState().runs.write.phase).toBe('done'))
  })

  it('freezes running Write parameters and admission identity across declaration edits and response loss', async () => {
    const parameters = [{
      name: 'output', type: 'string' as const, default: 'output.parquet',
    }]
    const bindings = [{ name: 'output', value: 'output.parquet' }]
    const write = NODE('write', 'write')
    write.data.config = {
      filename: { parameterRef: 'output' }, writeMode: 'overwrite',
    }
    const doc = {
      id: 'c', version: 1, name: 'test', requirements: [], parameters,
      nodes: [write], edges: [],
    }
    useStore.setState({ doc, runs: { write: {
      phase: 'idle', parameterBindings: bindings, parametersReady: true,
      parameterContractFingerprint: JSON.stringify(parameters),
    } } })
    const intent = {
      destination: {
        logicalUri: '/outputs/output.parquet', name: 'output',
        provider: 'managed-local-file' as const,
      },
      mode: 'create' as const, expectedSchema: [], expectedHead: null,
      idempotencyKey: 'parameter-write-key', partitions: [],
      provenance: { publication: {
        idempotencyKey: 'parameter-write-key', runId: 'run-write',
        producer: 'c', producerVersion: 1, stepId: 'write', provenance: 'run',
      }, parents: [] },
    }
    const admission = {
      nodeId: 'write', managed: true, destination: '/outputs/output.parquet',
      mode: 'create' as const, provider: 'managed-local-file',
      expectedSchema: [], partitions: [], intent,
    }
    apiMocks.writeAdmission.mockResolvedValueOnce(admission)
    let rejectRun!: (reason: Error) => void
    apiMocks.run.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      rejectRun = reject
    }))

    const pending = useStore.getState().run('write')
    await vi.waitFor(() => expect(apiMocks.run).toHaveBeenCalledTimes(1))
    const running = useStore.getState().runs.write
    expect(running.phase).toBe('running')

    expect(useStore.getState().setParameters([{
      ...parameters[0], default: 'next-output.parquet',
    }])).toBeNull()
    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'running',
      parameterBindings: running.parameterBindings,
      parametersReady: running.parametersReady,
      parameterContractFingerprint: running.parameterContractFingerprint,
      writeAdmission: running.writeAdmission,
      writeSubmissionId: running.writeSubmissionId,
      writeAdmissionFingerprint: running.writeAdmissionFingerprint,
    })

    rejectRun(new Error('network response lost'))
    await pending
    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'failed',
      parameterBindings: bindings,
      parameterContractFingerprint: JSON.stringify(parameters),
      writeAdmission: admission,
      writeSubmissionId: running.writeSubmissionId,
      writeAdmissionFingerprint: running.writeAdmissionFingerprint,
    })

    await useStore.getState().requestRun('write')
    expect(useStore.getState().runs.write).toMatchObject({
      phase: 'parameters',
      parametersReady: false,
      parameterContractFingerprint: JSON.stringify(useStore.getState().doc.parameters),
    })
    expect(apiMocks.estimate).not.toHaveBeenCalled()
  })

  it('does not turn provider-neutral sink retries into typed write recovery', async () => {
    const write = NODE('write', 'write')
    write.data.config = { filename: 'output.csv', writeMode: 'append' }
    useStore.setState({
      doc: { id: 'c', version: 1, name: 'test', requirements: [], nodes: [write], edges: [] },
    })
    apiMocks.writeAdmission.mockResolvedValueOnce({
      nodeId: 'write', managed: false, destination: '/outputs/output', mode: 'append',
      provider: 'duckdb', expectedSchema: [], partitions: [],
    })
    apiMocks.run.mockRejectedValueOnce(new Error('network response lost'))

    await useStore.getState().run('write')

    expect(useStore.getState().runs.write).toMatchObject({ phase: 'failed' })
    expect(useStore.getState().runs.write.writeAdmission).toBeUndefined()
    expect(useStore.getState().runs.write.writeSubmissionId).toBeUndefined()
  })

  it('reuses one submission id across bounded ambiguous submission retries', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    apiMocks.fullProfile
      .mockRejectedValueOnce(new Error('network response lost'))
      .mockRejectedValueOnce(new KernelError(503, 'hub restarting'))
      .mockResolvedValueOnce({
        runId: 'profile-adopted', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
        planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
        rowsProcessed: 10, ms: 10, placement: 'local', perNode: [],
        profile: { columns: [], rowCount: 10, sampled: false },
      })

    await useStore.getState().startFullProfile('source')

    expect(apiMocks.fullProfile).toHaveBeenCalledTimes(3)
    const submissionIds = apiMocks.fullProfile.mock.calls.map((call) => call[4])
    expect(new Set(submissionIds).size).toBe(1)
    expect(submissionIds[0]).toMatch(/^[0-9a-f-]{36}$/i)
    expect(useStore.getState().profileJobs.source).toMatchObject({
      submissionId: submissionIds[0], submissionUnresolved: false,
      identityVerified: true, phase: 'done',
    })
  })

  it('does not retry a non-ambiguous profile submission rejection', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    apiMocks.fullProfile.mockRejectedValueOnce(new KernelError(409, 'stale plan'))

    await useStore.getState().startFullProfile('source')

    expect(apiMocks.fullProfile).toHaveBeenCalledTimes(1)
    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'failed', submissionUnresolved: false, error: 'stale plan',
    })
  })

  it('keeps an ambiguous submission id for an explicit reconciliation retry', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    apiMocks.fullProfile.mockRejectedValue(new Error('connection reset'))

    await useStore.getState().startFullProfile('source')

    expect(apiMocks.fullProfile).toHaveBeenCalledTimes(3)
    const submissionId = useStore.getState().profileJobs.source.submissionId
    expect(useStore.getState().profileJobs.source.submissionUnresolved).toBe(true)
    apiMocks.fullProfile.mockReset().mockResolvedValueOnce({
      runId: 'profile-reconciled-later', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    })

    await useStore.getState().startFullProfile('source')

    expect(apiMocks.fullProfile).toHaveBeenCalledWith(
      doc, 'source', 'out', 'a'.repeat(64), submissionId, true,
    )
    expect(useStore.getState().profileJobs.source.status?.runId).toBe('profile-reconciled-later')
  })

  it('records cancel intent while submission is pending and cancels immediately after adoption', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    let finishSubmission!: (status: any) => void
    apiMocks.fullProfile.mockImplementationOnce(() => new Promise((resolve) => { finishSubmission = resolve }))
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))

    const submission = useStore.getState().startFullProfile('source')
    await vi.waitFor(() => expect(apiMocks.fullProfile).toHaveBeenCalledTimes(1))
    await useStore.getState().cancelFullProfile('source')
    expect(useStore.getState().profileJobs.source).toMatchObject({ phase: 'cancelling', cancelRequested: true })
    expect(apiMocks.cancelRun).not.toHaveBeenCalled()

    finishSubmission({
      runId: 'profile-cancel-after-adopt', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })
    await submission

    expect(apiMocks.cancelRun).toHaveBeenCalledWith('profile-cancel-after-adopt')
    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelled', cancelRequested: true, identityVerified: false,
      status: { runId: 'profile-cancel-after-adopt', status: 'cancelled' },
    })
  })

  it('reconciles a lost post-adoption cancellation response while the ordinary poll is hung', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    let finishSubmission!: (status: any) => void
    apiMocks.fullProfile.mockImplementationOnce(() => new Promise((resolve) => { finishSubmission = resolve }))
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))
    apiMocks.cancelRun.mockRejectedValueOnce(new Error('cancel response lost'))

    const submission = useStore.getState().startFullProfile('source')
    await vi.waitFor(() => expect(apiMocks.fullProfile).toHaveBeenCalledTimes(1))
    await useStore.getState().cancelFullProfile('source')
    finishSubmission({
      runId: 'profile-cancel-response-lost', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })
    await submission

    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(2)
    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelled', cancelRequested: true, identityVerified: false,
      status: { runId: 'profile-cancel-response-lost', status: 'cancelled' },
    })
    expect(useStore.getState().profileJobs.source.error).toBeUndefined()
  })

  it('rejects an async profile writeback after its submission id is superseded', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    let finishSubmission!: (status: any) => void
    apiMocks.fullProfile.mockImplementationOnce(() => new Promise((resolve) => { finishSubmission = resolve }))
    const submission = useStore.getState().startFullProfile('source')
    await vi.waitFor(() => expect(apiMocks.fullProfile).toHaveBeenCalledTimes(1))
    useStore.setState((state) => ({ profileJobs: { ...state.profileJobs, source: {
      ...state.profileJobs.source!, submissionId: 'newer-explicit-submission', status: undefined,
    } } }))

    finishSubmission({
      runId: 'superseded-submission-run', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })
    await submission

    expect(apiMocks.cancelRun).toHaveBeenCalledWith('superseded-submission-run')
    expect(useStore.getState().profileJobs.source).toMatchObject({ submissionId: 'newer-explicit-submission' })
    expect(useStore.getState().profileJobs.source.status).toBeUndefined()
  })

  it('reconciles and cancels an orphaned submission after the graph and preflight are superseded', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    let finishOrphan!: (status: any) => void
    apiMocks.fullProfile
      .mockRejectedValueOnce(new Error('response lost 1'))
      .mockRejectedValueOnce(new Error('response lost 2'))
      .mockRejectedValueOnce(new Error('response lost 3'))
      .mockImplementationOnce(() => new Promise((resolve) => { finishOrphan = resolve }))

    const firstSubmission = useStore.getState().startFullProfile('source')
    await vi.waitFor(() => expect(apiMocks.fullProfile).toHaveBeenCalledTimes(1))
    useStore.getState().updateConfig('source', { uri: 'new-source.parquet' })
    await firstSubmission
    await vi.waitFor(() => expect(apiMocks.fullProfile).toHaveBeenCalledTimes(4))
    const oldSubmissionId = apiMocks.fullProfile.mock.calls[0][4]
    expect(apiMocks.fullProfile.mock.calls.slice(0, 4).every((call) => call[4] === oldSubmissionId)).toBe(true)

    // The user can move on to the new graph. Orphan reconciliation owns the old captured doc/key and
    // must not write through this newer preflight when its response finally arrives.
    await useStore.getState().prepareFullProfile('source')
    const newGeneration = useStore.getState().profileJobs.source.requestGeneration
    expect(useStore.getState().profileJobs.source.phase).toBe('preflight')
    finishOrphan({
      runId: 'old-orphaned-scan', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })

    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledWith('old-orphaned-scan'))
    expect(useStore.getState().profileJobs.source).toMatchObject({
      requestGeneration: newGeneration, phase: 'preflight',
    })
    expect(useStore.getState().profileJobs.source.status).toBeUndefined()
  })

  it('stops orphan reconciliation before retrying under a different user', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    let rejectOrphan!: (error: Error) => void
    apiMocks.fullProfile
      .mockRejectedValueOnce(new Error('response lost 1'))
      .mockRejectedValueOnce(new Error('response lost 2'))
      .mockRejectedValueOnce(new Error('response lost 3'))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOrphan = reject }))

    const submission = useStore.getState().startFullProfile('source')
    await vi.waitFor(() => expect(apiMocks.fullProfile).toHaveBeenCalledTimes(1))
    useStore.getState().updateConfig('source', { uri: 'new-source.parquet' })
    await submission
    await vi.waitFor(() => expect(apiMocks.fullProfile).toHaveBeenCalledTimes(4))
    useStore.setState({ currentUser: { id: 'bob', name: 'Bob' } })
    rejectOrphan(new Error('old-user request lost'))

    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(apiMocks.fullProfile).toHaveBeenCalledTimes(4)
    expect(apiMocks.cancelRun).not.toHaveBeenCalled()
  })

  it('clears preflight and fails closed before submission when user identity disappears', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    useStore.setState({ currentUser: null })
    useStore.setState({ canvasRole: 'owner' })

    await useStore.getState().startFullProfile('source')

    expect(apiMocks.fullProfile).not.toHaveBeenCalled()
    expect(useStore.getState().profileJobs.source).toBeUndefined()
  })

  it('keeps detached cancellation supervised after a 200 running response and deduplicates the run', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    const running = {
      runId: 'removed-node-profile', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 1, ms: 10,
      placement: 'local', perNode: [],
    }
    const installActiveJob = () => useStore.setState({
      doc: { ...doc, nodes: [NODE('source')] },
      profileJobs: { source: {
        canvasId: doc.id, nodeId: 'source', principalId: 'alice', canCancel: true,
        planIdentity: JSON.stringify({}), planDigest,
        requestGeneration: 1, phase: 'running', identityVerified: true,
        status: running,
      } },
    } as any)
    let finishFirstCancel!: (status: any) => void
    apiMocks.cancelRun.mockImplementationOnce(() => new Promise((resolve) => { finishFirstCancel = resolve }))
    apiMocks.runStatus.mockResolvedValueOnce({ ...running, status: 'cancelled' })
    installActiveJob()

    useStore.getState().removeNode('source')
    // A second local detachment of the same exact run joins the existing supervisor.
    installActiveJob()
    useStore.getState().removeNode('source')

    expect(apiMocks.cancelRun).toHaveBeenCalledWith('removed-node-profile')
    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(1)
    expect(useStore.getState().profileJobs.source).toBeUndefined()
    expect(useStore.getState().doc.nodes).toHaveLength(0)

    // HTTP 200 is not an acknowledgement while the exact run is still active.
    finishFirstCancel(running)
    await vi.waitFor(() => expect(apiMocks.runStatus).toHaveBeenCalledWith('removed-node-profile'))
    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(1)

    // An exact terminal observation releases tracking, so a later detachment starts a new supervisor.
    installActiveJob()
    useStore.getState().removeNode('source')
    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledTimes(2))
  })

  it('stops a detached cancellation supervisor before replaying a run id under another user', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    const running = {
      runId: 'alice-detached-profile', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 1, ms: 10,
      placement: 'local', perNode: [],
    }
    useStore.setState({
      doc,
      profileJobs: { source: {
        canvasId: doc.id, nodeId: 'source', principalId: 'alice', canCancel: true,
        planIdentity: JSON.stringify({}), planDigest,
        requestGeneration: 1, phase: 'running', identityVerified: true, status: running,
      } },
    } as any)
    let finishCancel!: (status: any) => void
    apiMocks.cancelRun.mockImplementationOnce(() => new Promise((resolve) => { finishCancel = resolve }))

    useStore.getState().removeNode('source')
    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledWith(running.runId))
    useStore.setState({ currentUser: { id: 'bob', name: 'Bob' } })
    finishCancel(running)
    await Promise.resolve()
    await Promise.resolve()

    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(1)
    expect(apiMocks.runStatus).not.toHaveBeenCalledWith(running.runId)
  })

  it('permanently stops detached cancellation after a non-retryable authorization rejection', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    const running = {
      runId: 'detached-profile-role-revoked', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 1, ms: 10,
      placement: 'local', perNode: [],
    }
    useStore.setState({
      doc,
      profileJobs: { source: {
        canvasId: doc.id, nodeId: 'source', principalId: 'alice', canCancel: true,
        planIdentity: JSON.stringify({}), planDigest,
        requestGeneration: 1, phase: 'running', identityVerified: true, status: running,
      } },
    } as any)
    apiMocks.cancelRun.mockRejectedValueOnce(new KernelError(403, 'role changed to viewer'))

    useStore.getState().removeNode('source')
    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledWith(running.runId))
    await new Promise((resolve) => setTimeout(resolve, 150))

    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(1)
    expect(apiMocks.runStatus).not.toHaveBeenCalledWith(running.runId)
  })

  it('fails closed and cancels a malformed first profile response', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    apiMocks.fullProfile.mockResolvedValueOnce({
      runId: 'profile-malformed', status: 'running', jobType: 'profile', targetNodeId: 'other-node',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })

    await useStore.getState().startFullProfile('source')

    expect(apiMocks.cancelRun).toHaveBeenCalledWith('profile-malformed')
    expect(useStore.getState().profileJobs.source?.phase).toBe('failed')
    expect(useStore.getState().profileJobs.source?.error).toMatch(/invalid durable identity/i)
  })

  it('does not cancel an unrelated ordinary run returned by a malformed profile submission', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    apiMocks.fullProfile.mockResolvedValueOnce({
      runId: 'ordinary-run-not-ours', status: 'running', jobType: 'run', targetNodeId: 'source',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })

    await useStore.getState().startFullProfile('source')

    expect(apiMocks.cancelRun).not.toHaveBeenCalledWith('ordinary-run-not-ours')
    expect(useStore.getState().profileJobs.source?.phase).toBe('failed')
  })

  it('lets a terminal poll beat a rejected cancellation response for the same attempt', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    const running = {
      runId: 'profile-cancel-race', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.fullProfile.mockResolvedValueOnce(running)
    let finishPoll!: (status: any) => void
    apiMocks.runStatus.mockImplementationOnce(() => new Promise((resolve) => { finishPoll = resolve }))
    let rejectCancel!: (error: Error) => void
    apiMocks.cancelRun.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectCancel = reject }))

    await useStore.getState().startFullProfile('source')
    await vi.waitFor(() => expect(apiMocks.runStatus).toHaveBeenCalledWith(running.runId))
    const cancellation = useStore.getState().cancelFullProfile('source')
    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledWith(running.runId))
    finishPoll({
      ...running, status: 'done', rowsProcessed: 10,
      profile: { columns: [], rowCount: 10, sampled: false },
    })
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.phase).toBe('done'))
    rejectCancel(new Error('cancel response lost'))
    await cancellation

    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'done', status: { status: 'done', runId: running.runId },
    })
    expect(useStore.getState().profileJobs.source.error).toBeUndefined()
    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(1)
  })

  it('starts another cancel round after active cancel and status responses', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    const running = {
      runId: 'profile-cancel-active-200', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.fullProfile.mockResolvedValueOnce(running)
    let finishOrdinaryStatus!: (status: any) => void
    let finishSupervisorStatus!: (status: any) => void
    apiMocks.runStatus
      .mockImplementationOnce(() => new Promise((resolve) => { finishOrdinaryStatus = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishSupervisorStatus = resolve }))
      .mockImplementation(() => new Promise(() => {}))
    apiMocks.cancelRun.mockReset()
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce({ ...running, rowsProcessed: 2, ms: 20 })
      .mockResolvedValueOnce({ ...running, status: 'cancelled' })

    await useStore.getState().startFullProfile('source')
    await useStore.getState().cancelFullProfile('source')

    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(apiMocks.runStatus).toHaveBeenCalledTimes(2))
    finishOrdinaryStatus({ ...running, rowsProcessed: 2, ms: 20 })
    finishSupervisorStatus({ ...running, rowsProcessed: 3, ms: 30 })
    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledTimes(3))
    expect(apiMocks.cancelRun).toHaveBeenNthCalledWith(1, running.runId)
    expect(apiMocks.cancelRun).toHaveBeenNthCalledWith(2, running.runId)
    expect(apiMocks.cancelRun).toHaveBeenNthCalledWith(3, running.runId)
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelled', status: { runId: running.runId, status: 'cancelled' },
    }))
    await Promise.resolve()
    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(3)
  })

  it('reissues explicit cancellation after a transient rejection until an exact terminal response', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    const running = {
      runId: 'profile-cancel-transient-error', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.fullProfile.mockResolvedValueOnce(running)
    apiMocks.runStatus.mockImplementation(() => new Promise(() => {}))
    apiMocks.cancelRun.mockReset()
      .mockRejectedValueOnce(new Error('connection reset'))
      .mockResolvedValueOnce({ ...running, status: 'cancelled' })

    await useStore.getState().startFullProfile('source')
    await useStore.getState().cancelFullProfile('source')

    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledTimes(2))
    expect(apiMocks.cancelRun).toHaveBeenNthCalledWith(1, running.runId)
    expect(apiMocks.cancelRun).toHaveBeenNthCalledWith(2, running.runId)
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelled', status: { runId: running.runId, status: 'cancelled' },
    }))
    await Promise.resolve()
    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(2)
  })

  it('starts another cancel round after a transient supervisor status error', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    const running = {
      runId: 'profile-cancel-status-transient', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.fullProfile.mockResolvedValueOnce(running)
    apiMocks.runStatus
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockRejectedValueOnce(new Error('status connection reset'))
    apiMocks.cancelRun.mockReset()
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce({ ...running, rowsProcessed: 2, ms: 20 })
      .mockResolvedValueOnce({ ...running, status: 'cancelled' })

    await useStore.getState().startFullProfile('source')
    await useStore.getState().cancelFullProfile('source')

    await vi.waitFor(() => expect(apiMocks.runStatus).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledTimes(3))
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelled', status: { runId: running.runId, status: 'cancelled' },
    }))
    expect(apiMocks.cancelRun).toHaveBeenNthCalledWith(3, running.runId)
  })

  it('keeps a concurrent done terminal ahead of a delayed supervisor cancellation terminal', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    const running = {
      runId: 'profile-cancel-terminal-race', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    const done = {
      ...running, status: 'done', rowsProcessed: 10,
      profile: { columns: [], rowCount: 10, sampled: false },
    }
    apiMocks.fullProfile.mockResolvedValueOnce(running)
    let finishPoll!: (status: any) => void
    apiMocks.runStatus.mockImplementationOnce(() => new Promise((resolve) => { finishPoll = resolve }))
    let finishRetry!: (status: any) => void
    apiMocks.cancelRun.mockReset()
      .mockResolvedValueOnce(running)
      .mockImplementationOnce(() => new Promise((resolve) => { finishRetry = resolve }))

    await useStore.getState().startFullProfile('source')
    await useStore.getState().cancelFullProfile('source')
    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledTimes(2))

    finishPoll(done)
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'done', status: { runId: running.runId, status: 'done' },
    }))
    finishRetry({ ...running, status: 'cancelled' })
    await Promise.resolve()

    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'done', status: { runId: running.runId, status: 'done', profile: { rowCount: 10 } },
    })
    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(2)
  })

  it('sanitizes an unverified terminal reconciled by the tracked cancellation supervisor', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    const running = {
      runId: 'profile-cancel-unverified', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    }
    apiMocks.fullProfile.mockResolvedValueOnce(running)
    apiMocks.runStatus.mockImplementation(() => new Promise(() => {}))
    apiMocks.cancelRun.mockReset()
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce({
        ...running, status: 'cancelled', rowsProcessed: 999, totalRows: 999,
        profile: { columns: [], rowCount: 999, sampled: false },
        outputs: [{
          nodeId: 'source', portId: 'out', wire: 'dataset', publicationKind: 'result',
          outcome: 'committed', uri: '/must/not/leak.parquet', rows: 999,
        }],
      })

    await useStore.getState().startFullProfile('source')
    useStore.setState((state) => ({ profileJobs: { ...state.profileJobs, source: {
      ...state.profileJobs.source!, identityVerified: false,
    } } }))
    await useStore.getState().cancelFullProfile('source')

    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelled', identityVerified: false,
      status: { runId: running.runId, status: 'cancelled', rowsProcessed: 0, ms: 0, perNode: [] },
    }))
    expect(useStore.getState().profileJobs.source.status?.profile).toBeUndefined()
    expect(useStore.getState().profileJobs.source.status?.outputs).toEqual([])
  })

  it('reconciles a compact cancellation terminal through the durable exact-attempt projection', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    const running = {
      runId: 'profile-cancel-compact-terminal', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    const cancelled = { ...running, status: 'cancelled' }
    apiMocks.fullProfile.mockResolvedValueOnce(running)
    apiMocks.runStatus.mockImplementation(() => new Promise(() => {}))
    apiMocks.cancelRun.mockReset().mockResolvedValueOnce({
      runId: running.runId, status: 'cancelled', jobType: 'run',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })
    apiMocks.profileJobs.mockResolvedValueOnce([cancelled])

    await useStore.getState().startFullProfile('source')
    await useStore.getState().cancelFullProfile('source')

    expect(apiMocks.profileJobs).toHaveBeenCalledWith(doc.id)
    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelled', identityVerified: true,
      status: {
        runId: running.runId, status: 'cancelled', jobType: 'profile',
        planDigest: running.planDigest, profileAttemptOrder: 1,
      },
    })
    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(1)
  })

  it('does not retry explicit cancellation after a non-retryable authorization rejection', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    const running = {
      runId: 'profile-cancel-role-revoked', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.fullProfile.mockResolvedValueOnce(running)
    apiMocks.runStatus.mockImplementation(() => new Promise(() => {}))
    apiMocks.cancelRun.mockReset().mockRejectedValueOnce(new KernelError(403, 'role revoked'))

    await useStore.getState().startFullProfile('source')
    await useStore.getState().cancelFullProfile('source')

    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(apiMocks.cancelRun).toHaveBeenCalledTimes(1)
    expect(apiMocks.cancelRun).toHaveBeenCalledWith(running.runId)
  })

  it('keeps monitoring a nonterminal run when cancellation transport fails', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    const running = {
      runId: 'profile-cancel-unknown', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.fullProfile.mockResolvedValueOnce(running)
    let finishPoll!: (status: any) => void
    let finishSupervisorStatus!: (status: any) => void
    apiMocks.runStatus
      .mockImplementationOnce(() => new Promise((resolve) => { finishPoll = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishSupervisorStatus = resolve }))
    apiMocks.cancelRun.mockReset()
      .mockRejectedValueOnce(new Error('connection reset'))
      .mockRejectedValueOnce(new Error('connection reset again'))

    await useStore.getState().startFullProfile('source')
    const cancellation = useStore.getState().cancelFullProfile('source')
    await cancellation

    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelling', cancelRequested: true,
      status: { runId: running.runId, status: 'running' },
    })
    expect(useStore.getState().profileJobs.source.error).toMatch(/could not be confirmed/i)

    await vi.waitFor(() => expect(apiMocks.runStatus).toHaveBeenCalledTimes(2))
    finishPoll({ ...running, rowsProcessed: 2, ms: 20 })
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source.status?.rowsProcessed).toBe(2))
    expect(useStore.getState().profileJobs.source.phase).toBe('cancelling')
    expect(useStore.getState().profileJobs.source.error).toMatch(/could not be confirmed/i)

    finishSupervisorStatus({ ...running, status: 'cancelled', rowsProcessed: 2, ms: 20 })
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelled', status: { runId: running.runId, status: 'cancelled' },
    }))
  })

  it('recovers a current result, then ignores a stale terminal-only response after reopen', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    // Capture the real digest without coupling this regression to identity serialization details.
    useStore.setState({ doc: current })
    await useStore.getState().prepareFullProfile('source')
    const planDigest = useStore.getState().profileJobs.source.planDigest
    useStore.setState({ profileJobs: {} })
    apiMocks.profileJobs.mockResolvedValueOnce([{
      runId: 'finished-away', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 4, totalRows: 4, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 4, sampled: false },
    }])
    apiMocks.activeRuns.mockRejectedValueOnce(new Error('transient active-run lookup failure'))

    useStore.getState().loadDoc(current, 'owner')
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.status?.runId).toBe('finished-away'))
    expect(useStore.getState().profileJobs.source?.phase).toBe('done')

    apiMocks.profileJobs.mockResolvedValueOnce([{
      runId: 'stale-plan', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: '0'.repeat(64), profileAttemptOrder: 2, rowsProcessed: 4, totalRows: 4, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 4, sampled: false },
    }])
    useStore.getState().loadDoc(current, 'owner')
    await vi.waitFor(() => expect(apiMocks.profileJobs).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(apiMocks.profileIdentity).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toBeUndefined())
    expect(apiMocks.cancelRun).not.toHaveBeenCalledWith('stale-plan')
  })

  it.each([
    ['stale terminal first', 'done', true],
    ['stale terminal last', 'done', false],
    ['stale active first', 'running', true],
    ['stale active last', 'running', false],
  ] as const)(
    'selects the current server digest across async recovery order: %s',
    async (_label, staleStatus, staleFirst) => {
      const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
      const currentAttempt = {
        runId: 'current-plan-order-1', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
        planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
        rowsProcessed: 4, totalRows: 4, ms: 10, placement: 'local', perNode: [],
        profile: { columns: [], rowCount: 4, sampled: false },
      }
      const staleAttempt = {
        runId: `stale-plan-order-2-${staleStatus}-${staleFirst ? 'first' : 'last'}`,
        status: staleStatus, jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
        planDigest: 'b'.repeat(64), profileAttemptOrder: 2,
        rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      }
      apiMocks.profileJobs.mockResolvedValueOnce(
        staleFirst ? [staleAttempt, currentAttempt] : [currentAttempt, staleAttempt],
      )

      useStore.getState().loadDoc(current, 'owner')

      await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
        phase: 'done', identityVerified: true, planDigest: currentAttempt.planDigest,
        status: { runId: currentAttempt.runId, profileAttemptOrder: 1 },
      }))
      expect(useStore.getState().profileJobs.source.status?.profile).toMatchObject({ rowCount: 4 })
      if (staleStatus === 'running') {
        await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledWith(staleAttempt.runId))
      } else {
        expect(apiMocks.cancelRun).not.toHaveBeenCalledWith(staleAttempt.runId)
      }
      expect(apiMocks.cancelRun).not.toHaveBeenCalledWith(currentAttempt.runId)
    },
  )

  it('ignores a stale active recovery for viewers without issuing cancellation mutations', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const stale = {
      runId: 'viewer-stale-active', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'b'.repeat(64), profileAttemptOrder: 2,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.profileJobs.mockResolvedValueOnce([stale])

    useStore.getState().loadDoc(current, 'viewer')

    await vi.waitFor(() => expect(apiMocks.profileIdentity).toHaveBeenCalled())
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toBeUndefined())
    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(apiMocks.cancelRun).not.toHaveBeenCalledWith(stale.runId)
  })

  it('recovers and read-only polls a current active profile for viewers', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const active = {
      runId: 'viewer-current-active', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 2,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.profileJobs.mockResolvedValueOnce([active])
    apiMocks.runStatus.mockResolvedValueOnce({ ...active, status: 'cancelled' })

    useStore.getState().loadDoc(current, 'viewer')

    await vi.waitFor(() => expect(apiMocks.runStatus).toHaveBeenCalledWith(active.runId))
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      principalId: 'alice', canCancel: false, phase: 'cancelled',
      status: { runId: active.runId, status: 'cancelled' },
    }))
    expect(apiMocks.cancelRun).not.toHaveBeenCalledWith(active.runId)
  })

  it('describes provisional viewer recovery without promising cancellation', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const active = {
      runId: 'viewer-verifying-active', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 2,
      rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.profileJobs.mockResolvedValueOnce([active])
    let finishIdentity!: (identity: { planDigest: string }) => void
    apiMocks.profileIdentity.mockImplementationOnce(() => new Promise((resolve) => { finishIdentity = resolve }))
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))

    useStore.getState().loadDoc(current, 'viewer')

    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'verifying', identityVerified: false, canCancel: false,
      status: { runId: active.runId },
    }))
    expect(useStore.getState().profileJobs.source.error).toBeUndefined()
    expect(apiMocks.cancelRun).not.toHaveBeenCalledWith(active.runId)

    finishIdentity({ targetPortId: 'out', planDigest: active.planDigest })
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'running', identityVerified: true, canCancel: false,
      status: { runId: active.runId },
    }))
  })

  it.each([
    ['owner', true],
    ['viewer', false],
  ] as const)('keeps a recovered terminal result in non-error verification for %s', async (role, canCancel) => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const terminal = {
      runId: `terminal-verifying-${role}`, status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 2,
      rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    }
    apiMocks.profileJobs.mockResolvedValueOnce([terminal])
    let finishIdentity!: (identity: { planDigest: string }) => void
    apiMocks.profileIdentity.mockImplementationOnce(() => new Promise((resolve) => { finishIdentity = resolve }))

    useStore.getState().loadDoc(current, role)

    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'verifying', identityVerified: false, canCancel,
      status: { runId: terminal.runId, status: 'done' },
    }))
    expect(useStore.getState().profileJobs.source.status?.profile).toBeUndefined()
    expect(useStore.getState().profileJobs.source.error).toBeUndefined()
    expect(apiMocks.cancelRun).not.toHaveBeenCalledWith(terminal.runId)

    finishIdentity({ targetPortId: 'out', planDigest: terminal.planDigest })
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'done', identityVerified: true, canCancel,
      status: { runId: terminal.runId, profile: { rowCount: 10 } },
    }))
  })

  it('clears principal-bound profile state immediately and rejects late recovery after identity switch', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    let finishRecovery!: (statuses: any[]) => void
    apiMocks.profileJobs.mockImplementationOnce(() => new Promise((resolve) => { finishRecovery = resolve }))
    useStore.getState().loadDoc(current, 'owner')
    const done = {
      runId: 'alice-complete-profile', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    }
    useStore.setState({
      profileJobs: { source: {
        canvasId: current.id, nodeId: 'source', principalId: 'alice', canCancel: true,
        planIdentity: JSON.stringify({}), planDigest: done.planDigest,
        requestGeneration: 1, phase: 'done', identityVerified: true, status: done,
      } },
    } as any)

    useStore.setState({ currentUser: { id: 'bob', name: 'Bob' } })
    expect(useStore.getState().profileJobs).toEqual({})
    finishRecovery([done])
    await Promise.resolve()
    await Promise.resolve()

    expect(useStore.getState().profileJobs).toEqual({})
    expect(apiMocks.cancelRun).not.toHaveBeenCalledWith(done.runId)
  })

  it('keeps a recovered active run cancellable while identity retry is pending, then verifies it', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const recovered = {
      runId: 'identity-retry-active', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 2,
      rowsProcessed: 2, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 2, sampled: false },
      outputs: [{
        nodeId: 'source', portId: 'out', wire: 'dataset', publicationKind: 'catalog',
        outcome: 'committed', uri: '/unverified/result.parquet', table: 'unverified', rows: 2,
      }],
    }
    apiMocks.activeRuns.mockResolvedValueOnce([recovered])
    let finishIdentity!: (identity: { planDigest: string }) => void
    apiMocks.profileIdentity
      .mockRejectedValueOnce(new Error('identity service warming'))
      .mockImplementationOnce(() => new Promise((resolve) => { finishIdentity = resolve }))
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))

    useStore.getState().loadDoc(current, 'owner')

    await vi.waitFor(() => expect(apiMocks.profileIdentity).toHaveBeenCalledTimes(2))
    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'verifying', identityVerified: false,
      status: { runId: recovered.runId, profileAttemptOrder: 2 },
    })
    expect(useStore.getState().profileJobs.source.status?.profile).toBeUndefined()
    expect(useStore.getState().profileJobs.source.status?.outputs).toEqual([])
    expect(useStore.getState().profileJobs.source.status).toMatchObject({
      rowsProcessed: 0, ms: 0, perNode: [],
    })
    expect(useStore.getState().profileJobs.source.status?.totalRows).toBeUndefined()
    expect(useStore.getState().profileJobs.source.error).toBeUndefined()

    finishIdentity({ targetPortId: 'out', planDigest: 'a'.repeat(64) })
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'running', identityVerified: true,
      status: { runId: recovered.runId, profile: { rowCount: 2 } },
    }))
  })

  it('fails closed after persistent identity failure while retaining exact cancellation identity', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const recovered = {
      runId: 'identity-failed-active', status: 'queued', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 4,
      rowsProcessed: 0, totalRows: 10, ms: 0, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 999, sampled: false },
      outputs: [{
        nodeId: 'source', portId: 'out', wire: 'dataset', publicationKind: 'catalog',
        outcome: 'committed', uri: '/must/not/leak.parquet', table: 'must_not_leak', rows: 999,
      }],
    }
    apiMocks.activeRuns.mockResolvedValueOnce([recovered])
    apiMocks.profileIdentity.mockRejectedValue(new Error('identity unavailable'))
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))
    apiMocks.cancelRun.mockRejectedValueOnce(new Error('cancel response lost'))

    useStore.getState().loadDoc(current, 'owner')

    await vi.waitFor(() => expect(apiMocks.profileIdentity).toHaveBeenCalledTimes(3))
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.error).toMatch(/could not verify/i))
    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'failed', identityVerified: false,
      status: {
        runId: recovered.runId, status: 'queued', targetNodeId: 'source',
        planDigest: recovered.planDigest, profileAttemptOrder: 4,
      },
    })
    expect(useStore.getState().profileJobs.source.status?.profile).toBeUndefined()
    expect(useStore.getState().profileJobs.source.status?.outputs).toEqual([])
    expect(useStore.getState().profileJobs.source.status).toMatchObject({
      rowsProcessed: 0, ms: 0, perNode: [],
    })
    expect(useStore.getState().profileJobs.source.status?.totalRows).toBeUndefined()
    expect(useStore.getState().profileJobs.source.status?.error).toBeUndefined()

    await useStore.getState().cancelFullProfile('source')
    expect(apiMocks.cancelRun).toHaveBeenCalledWith(recovered.runId)
    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'cancelled', identityVerified: false,
      status: { runId: recovered.runId, status: 'cancelled' },
    })
    expect(useStore.getState().profileJobs.source.status).toMatchObject({
      status: 'cancelled', rowsProcessed: 0, ms: 0, perNode: [],
    })
    expect(useStore.getState().profileJobs.source.status?.profile).toBeUndefined()
  })

  it('falls back to active profile recovery when the latest-profile request fails', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc: current })
    await useStore.getState().prepareFullProfile('source')
    const planDigest = useStore.getState().profileJobs.source.planDigest
    useStore.setState({ profileJobs: {} })
    apiMocks.profileJobs.mockRejectedValueOnce(new Error('transient latest-profile lookup failure'))
    apiMocks.activeRuns.mockResolvedValueOnce([{
      runId: 'active-fallback', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }])
    let finishStatus!: (status: any) => void
    apiMocks.runStatus.mockImplementationOnce(() => new Promise((resolve) => { finishStatus = resolve }))

    useStore.getState().loadDoc(current, 'owner')

    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('active-fallback'))
    expect(useStore.getState().profileJobs.source?.phase).toBe('running')
    finishStatus({
      runId: 'active-fallback', status: 'cancelled', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    })
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.phase).toBe('cancelled'))
  })

  it('does not let a delayed queued poll response regress a running profile', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    const base = {
      runId: 'poll-monotonic', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 2, rowsProcessed: 1, totalRows: 10,
      ms: 10, placement: 'local', perNode: [],
    }
    apiMocks.profileJobs.mockRejectedValueOnce(new Error('projection unavailable'))
    apiMocks.activeRuns.mockResolvedValueOnce([{ ...base, status: 'running' }])
    apiMocks.runStatus
      .mockResolvedValueOnce({ ...base, status: 'queued', rowsProcessed: 0 })
      .mockResolvedValueOnce({ ...base, status: 'cancelled' })

    useStore.getState().loadDoc(current, 'owner')
    await vi.waitFor(() => expect(apiMocks.runStatus).toHaveBeenCalledTimes(1))
    await Promise.resolve()
    await Promise.resolve()
    expect(useStore.getState().profileJobs.source?.phase).toBe('running')
    await vi.waitFor(
      () => expect(useStore.getState().profileJobs.source?.phase).toBe('cancelled'),
      { timeout: 1000 },
    )
  })

  it('fails closed when a profile poll returns a different durable identity', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    apiMocks.profileJobs.mockRejectedValueOnce(new Error('projection unavailable'))
    apiMocks.activeRuns.mockResolvedValueOnce([{
      runId: 'poll-identity', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 3, rowsProcessed: 1, totalRows: 10,
      ms: 10, placement: 'local', perNode: [],
    }])
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'poll-identity', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest: '0'.repeat(64), profileAttemptOrder: 3, rowsProcessed: 10, totalRows: 10,
      ms: 20, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    })

    useStore.getState().loadDoc(current, 'owner')
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.phase).toBe('failed'))
    expect(useStore.getState().profileJobs.source?.status?.status).toBe('running')
    expect(useStore.getState().profileJobs.source?.error).toMatch(/identity changed/i)
    expect(apiMocks.profileJobs).toHaveBeenCalledTimes(1)
  })

  it('recovers full profile detail from the durable projection after RunState pruning', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    const running = {
      runId: 'profile-pruned-detail', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 7, rowsProcessed: 3, totalRows: 10,
      ms: 10, placement: 'local', perNode: [],
    }
    const projected = {
      ...running, status: 'done', rowsProcessed: 10, ms: 30,
      profile: { columns: [], rowCount: 10, sampled: false },
    }
    apiMocks.activeRuns.mockResolvedValueOnce([running])
    apiMocks.profileJobs
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([projected])
    // Detail retention has evicted the RunState, so the run endpoint can return only its compact fence.
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: running.runId, status: 'done', jobType: 'run', rowsProcessed: 0,
      ms: 0, placement: 'local', perNode: [], error: 'terminal_details_pruned',
    })

    useStore.getState().loadDoc(current, 'owner')

    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.phase).toBe('done'))
    expect(useStore.getState().profileJobs.source?.status).toMatchObject({
      runId: running.runId, jobType: 'profile', profileAttemptOrder: 7,
      profile: { rowCount: 10, sampled: false },
    })
    expect(apiMocks.profileJobs).toHaveBeenCalledTimes(2)
  })

  it('recovers an authoritative profile while active-runs remains pending', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc: current })
    await useStore.getState().prepareFullProfile('source')
    const planDigest = useStore.getState().profileJobs.source.planDigest
    useStore.setState({ profileJobs: {} })
    let finishActive!: (statuses: any[]) => void
    apiMocks.activeRuns.mockImplementationOnce(() => new Promise((resolve) => { finishActive = resolve }))
    apiMocks.profileJobs.mockResolvedValueOnce([{
      runId: 'projection-first', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    }])

    useStore.getState().loadDoc(current, 'owner')

    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('projection-first'))
    finishActive([])
  })

  it('does not let an empty projection suppress a delayed active profile', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    let finishActive!: (statuses: any[]) => void
    apiMocks.activeRuns.mockImplementationOnce(() => new Promise((resolve) => { finishActive = resolve }))
    apiMocks.profileJobs.mockResolvedValueOnce([])
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))

    useStore.getState().loadDoc(current, 'owner')
    await vi.waitFor(() => expect(apiMocks.profileJobs).toHaveBeenCalled())
    finishActive([{
      runId: 'active-after-empty', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 3, rowsProcessed: 1, totalRows: 10,
      ms: 10, placement: 'local', perNode: [],
    }])

    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('active-after-empty'))
  })

  it('lets a newer active retry supersede an older projection for the same plan', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    let finishActive!: (statuses: any[]) => void
    apiMocks.activeRuns.mockImplementationOnce(() => new Promise((resolve) => { finishActive = resolve }))
    apiMocks.profileJobs.mockResolvedValueOnce([{
      runId: 'old-projection', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 4, rowsProcessed: 10, totalRows: 10,
      ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    }])
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))

    useStore.getState().loadDoc(current, 'owner')
    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('old-projection'))
    finishActive([{
      runId: 'new-active-retry', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 5, rowsProcessed: 1, totalRows: 10,
      ms: 10, placement: 'local', perNode: [],
    }])

    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('new-active-retry'))
  })

  it('never regresses the same recovered attempt from running to queued', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    let finishProjection!: (statuses: any[]) => void
    apiMocks.profileJobs.mockImplementationOnce(() => new Promise((resolve) => { finishProjection = resolve }))
    apiMocks.activeRuns.mockResolvedValueOnce([{
      runId: 'same-attempt', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 6, rowsProcessed: 1, totalRows: 10,
      ms: 10, placement: 'local', perNode: [],
    }])
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))

    useStore.getState().loadDoc(current, 'owner')
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.phase).toBe('running'))
    finishProjection([{
      runId: 'same-attempt', status: 'queued', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 6, rowsProcessed: 0,
      ms: 0, placement: 'local', perNode: [],
    }])
    await Promise.resolve()
    await Promise.resolve()

    expect(useStore.getState().profileJobs.source?.phase).toBe('running')
  })

  it('uses active profile provisionally while projection is pending, then yields to projection', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc: current })
    await useStore.getState().prepareFullProfile('source')
    const planDigest = useStore.getState().profileJobs.source.planDigest
    useStore.setState({ profileJobs: {} })
    let finishProjection!: (statuses: any[]) => void
    apiMocks.profileJobs.mockImplementationOnce(() => new Promise((resolve) => { finishProjection = resolve }))
    apiMocks.activeRuns.mockResolvedValueOnce([{
      runId: 'provisional-active', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }])
    let finishStatus!: (status: any) => void
    apiMocks.runStatus.mockImplementationOnce(() => new Promise((resolve) => { finishStatus = resolve }))

    useStore.getState().loadDoc(current, 'owner')

    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('provisional-active'))
    finishProjection([{
      runId: 'authoritative-terminal', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 2, rowsProcessed: 10, totalRows: 10, ms: 20, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    }])
    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('authoritative-terminal'))
    finishStatus({
      runId: 'provisional-active', status: 'running', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 2, totalRows: 10, ms: 20, placement: 'local', perNode: [],
    })
    await vi.waitFor(() => expect(apiMocks.cancelRun).toHaveBeenCalledWith('provisional-active'))
    expect(useStore.getState().profileJobs.source?.status?.runId).toBe('authoritative-terminal')
  })

  it('ignores an older same-canvas reattach response after a newer reload', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc: current })
    await useStore.getState().prepareFullProfile('source')
    const planDigest = useStore.getState().profileJobs.source.planDigest
    useStore.setState({ profileJobs: {} })
    let finishOld!: (statuses: any[]) => void
    let finishNew!: (statuses: any[]) => void
    apiMocks.profileJobs
      .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishNew = resolve }))

    useStore.getState().loadDoc(current, 'owner')
    useStore.getState().loadDoc(current, 'owner')
    finishNew([{
      runId: 'newer-reattach', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 2, rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    }])
    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('newer-reattach'))
    finishOld([{
      runId: 'older-reattach', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 5, totalRows: 5, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 5, sampled: false },
    }])
    await Promise.resolve()
    await Promise.resolve()
    expect(useStore.getState().profileJobs.source?.status?.runId).toBe('newer-reattach')
  })

  it('does not let delayed recovery replace a profile the user started after reopen', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    let finishRecovery!: (statuses: any[]) => void
    let finishSubmission!: (status: any) => void
    apiMocks.profileJobs.mockImplementationOnce(() => new Promise((resolve) => { finishRecovery = resolve }))
    apiMocks.fullProfile.mockImplementationOnce(() => new Promise((resolve) => { finishSubmission = resolve }))

    useStore.getState().loadDoc(current, 'owner')
    await useStore.getState().prepareFullProfile('source')
    const submission = useStore.getState().startFullProfile('source')
    finishRecovery([{
      runId: 'old-recovered-profile', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 5, totalRows: 5,
      ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 5, sampled: false },
    }])
    await Promise.resolve()
    await Promise.resolve()
    expect(useStore.getState().profileJobs.source?.phase).toBe('queued')
    expect(useStore.getState().profileJobs.source?.status).toBeUndefined()

    finishSubmission({
      runId: 'new-user-profile', status: 'done', jobType: 'profile', targetNodeId: 'source', targetPortId: 'out',
      planDigest, profileAttemptOrder: 2, rowsProcessed: 10, totalRows: 10,
      ms: 20, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    })
    await submission

    expect(useStore.getState().profileJobs.source?.status?.runId).toBe('new-user-profile')
    expect(apiMocks.cancelRun).not.toHaveBeenCalledWith('new-user-profile')
  })

  it('blocks a preview response when the graph topology changes', async () => {
    let finish!: (result: ReturnType<typeof previewResult>) => void
    apiMocks.preview.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    useStore.setState({
      doc: {
        id: 'c', version: 1, name: 'test', requirements: [],
        nodes: [NODE('source'), NODE('filter', 'filter')],
        edges: [{ id: 'source-filter', source: 'source', target: 'filter', data: { wire: 'dataset' } }],
      },
    })

    const pending = useStore.getState().runPreview('filter')
    useStore.getState().removeEdge('source-filter')
    finish(previewResult('old topology'))
    await pending

    expect(useStore.getState().previews.filter?.result).toBeUndefined()
  })

  it('keeps only the latest preview or pagination response for a node', async () => {
    let finishFirst!: (result: ReturnType<typeof previewResult>) => void
    let finishSecond!: (result: ReturnType<typeof previewResult>) => void
    apiMocks.preview
      .mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishSecond = resolve }))
    useStore.setState({ doc: { id: 'c', version: 1, name: 'test', nodes: [NODE('source')], edges: [], requirements: [] } })

    const first = useStore.getState().runPreview('source', 0)
    const second = useStore.getState().runPreview('source', 50)
    finishSecond(previewResult('newer'))
    await second
    const latestGeneration = useStore.getState().previews.source?.requestGeneration
    expect(useStore.getState().previews.source).toMatchObject({ offset: 50, result: previewResult('newer') })

    finishFirst(previewResult('older'))
    await first
    expect(useStore.getState().previews.source).toMatchObject({
      requestGeneration: latestGeneration, offset: 50, result: previewResult('newer'),
    })
  })

  it('does not install an in-flight preview after a canvas switch or node deletion', async () => {
    let finishCanvas!: (result: ReturnType<typeof previewResult>) => void
    let finishDeleted!: (result: ReturnType<typeof previewResult>) => void
    apiMocks.preview
      .mockImplementationOnce(() => new Promise((resolve) => { finishCanvas = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishDeleted = resolve }))
    useStore.setState({ doc: { id: 'c', version: 1, name: 'test', nodes: [NODE('source')], edges: [], requirements: [] } })

    const onOldCanvas = useStore.getState().runPreview('source')
    useStore.setState({ doc: emptyTestDoc('other'), previews: {} })
    finishCanvas(previewResult('old canvas'))
    await onOldCanvas
    expect(useStore.getState().previews).toEqual({})

    useStore.setState({ doc: { id: 'other', version: 1, name: 'other', nodes: [NODE('source')], edges: [], requirements: [] } })
    const onDeletedNode = useStore.getState().runPreview('source')
    useStore.getState().removeNode('source')
    finishDeleted(previewResult('deleted node'))
    await onDeletedNode
    expect(useStore.getState().previews.source).toBeUndefined()
  })

  it('pushToast adds a toast and dismissToast removes it', () => {
    useStore.getState().pushToast('boom', 'error')
    const t = useStore.getState().toasts.find((x) => x.msg === 'boom')
    expect(t?.kind).toBe('error')
    useStore.getState().dismissToast(t!.id)
    expect(useStore.getState().toasts.some((x) => x.msg === 'boom')).toBe(false)
  })

  it('refreshes a stale editor role and installs the server-confirmed viewer role before reopening', async () => {
    const doc = { id: 'shared', version: 1, name: 'shared', nodes: [NODE('a')], edges: [] }
    apiMocks.getCanvas.mockResolvedValue(doc)
    useStore.setState({ files: [{ id: 'shared', name: 'shared', version: 1, role: 'editor' }] })
    apiMocks.listCanvases.mockResolvedValue([{ id: 'shared', name: 'shared', version: 1, role: 'viewer' }])

    expect(await useStore.getState().openFile('shared')).toBe(true)
    const before = useStore.getState().doc

    expect(useStore.getState().canvasRole).toBe('viewer')
    expect(useStore.getState().addNode('source', { x: 10, y: 10 })).toBeNull()
    useStore.getState().setNodes([])
    useStore.getState().updateConfig('a', { uri: 'changed' })
    useStore.getState().renameFile('changed')
    useStore.getState().applyAgentGraph({ nodes: [NODE('replacement')], edges: [] })

    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().past).toHaveLength(0)
  })

  it('preserves a valid node selection when reopening the same canvas', async () => {
    const doc = { id: 'shared', version: 2, name: 'shared', nodes: [NODE('a')], edges: [] }
    useStore.setState({
      doc: { ...doc, version: 1 },
      selectedId: 'a',
      selectedIds: ['a'],
      firstRunChoice: true,
    })
    apiMocks.getCanvas.mockResolvedValue(doc)
    apiMocks.listCanvases.mockResolvedValue([
      { id: 'shared', name: 'shared', version: 2, role: 'owner' },
    ])

    expect(await useStore.getState().openFile('shared')).toBe(true)

    expect(useStore.getState().selectedId).toBe('a')
    expect(useStore.getState().firstRunChoice).toBe(false)
  })

  it('requests one initial viewport fit for a non-empty saved Canvas unless a node deep link owns the view', async () => {
    const doc = { id: 'saved', version: 2, name: 'saved', nodes: [NODE('a')], edges: [] }
    apiMocks.getCanvas.mockResolvedValue(doc)
    apiMocks.listCanvases.mockResolvedValue([
      { id: 'saved', name: 'saved', version: 2, role: 'owner' },
    ])

    expect(await useStore.getState().openFile('saved')).toBe(true)
    const fit = useStore.getState().viewportFitRequest
    expect(fit).toMatchObject({ canvasId: 'saved' })
    expect(fit?.documentIdentity).toBe(canvasViewportDocumentIdentity(doc))

    useStore.getState().acknowledgeViewportFit(fit!.id)
    expect(await useStore.getState().openFile('saved', { skipViewportFit: true })).toBe(true)
    expect(useStore.getState().viewportFitRequest).toBeNull()

    const empty = { id: 'empty', version: 1, name: 'empty', nodes: [], edges: [] }
    apiMocks.getCanvas.mockResolvedValue(empty)
    apiMocks.listCanvases.mockResolvedValue([
      { id: 'empty', name: 'empty', version: 1, role: 'owner' },
    ])
    expect(await useStore.getState().openFile('empty')).toBe(true)
    expect(useStore.getState().viewportFitRequest).toBeNull()
  })

  it('fits a recovered non-empty local draft unless a node deep link owns the view', async () => {
    const doc = { id: 'local-saved', version: 3, name: 'local saved', nodes: [NODE('a'), NODE('b')], edges: [] }
    expect(writeCanvasDraft({
      draftId: doc.id,
      principalId: 'alice',
      canvasId: doc.id,
      baseCanvasId: doc.id,
      baseVersion: doc.version,
      name: doc.name,
      doc,
      createAttemptDoc: null,
      syncState: 'dirty',
      lastLocalEditAt: '2026-07-23T15:00:00.000Z',
    }).ok).toBe(true)
    useStore.getState().refreshLocalDrafts()

    expect(useStore.getState().openLocalDraft(doc.id)).toBe(true)
    const fit = useStore.getState().viewportFitRequest
    expect(fit).toMatchObject({ canvasId: doc.id })
    expect(fit?.documentIdentity).toBe(canvasViewportDocumentIdentity(doc))
    expect(apiMocks.activeRuns).toHaveBeenCalledWith(doc.id)
    expect(apiMocks.profileJobs).toHaveBeenCalledWith(doc.id)

    useStore.getState().acknowledgeViewportFit(fit!.id)
    expect(await useStore.getState().openFile(doc.id, { skipViewportFit: true })).toBe(true)
    expect(useStore.getState().viewportFitRequest).toBeNull()
    expect(apiMocks.getCanvas).not.toHaveBeenCalled()
  })

  it('lets only the latest overlapping file-open navigation install a document', async () => {
    let finishA!: (doc: ReturnType<typeof emptyTestDoc>) => void
    const a = new Promise<ReturnType<typeof emptyTestDoc>>((resolve) => { finishA = resolve })
    apiMocks.getCanvas.mockImplementation((id: string) => id === 'a' ? a : Promise.resolve(emptyTestDoc('b')))
    apiMocks.listCanvases.mockResolvedValue([{ id: 'b', name: 'b', version: 1, role: 'owner' }])

    const openA = useStore.getState().openFile('a')
    const openB = useStore.getState().openFile('b')
    expect(await openB).toBe(true)
    finishA(emptyTestDoc('a'))

    expect(await openA).toBe(false)
    expect(useStore.getState().doc.id).toBe('b')
    expect(useStore.getState().canvasRole).toBe('owner')
  })

  it.each(['resolve', 'reject'] as const)(
    'does not let a stale transform-reference %s write reclaim an explicit Inbox destination',
    async (settlement) => {
      let resolveReferences!: (references: CanvasTransformReference[]) => void
      let rejectReferences!: (error: Error) => void
      apiMocks.getCanvas.mockResolvedValue(emptyTestDoc('deferred-references'))
      apiMocks.listCanvases.mockResolvedValue([
        { id: 'deferred-references', name: 'deferred-references', version: 1, role: 'owner' },
      ])
      apiMocks.canvasTransformReferences.mockImplementationOnce(() => new Promise((resolve, reject) => {
        resolveReferences = resolve
        rejectReferences = reject
      }))

      const opening = useStore.getState().openFile('deferred-references')
      await vi.waitFor(() => expect(apiMocks.canvasTransformReferences).toHaveBeenCalledWith('deferred-references'))
      useStore.getState().setInboxQuery('status=unread')
      const ownedReferences: CanvasTransformReference[] = [{
        id: 'newer-destination', version: 'v1', nodeIds: [], availability: 'active',
      }]
      useStore.setState({ canvasTransformReferences: ownedReferences })

      if (settlement === 'resolve') resolveReferences([])
      else rejectReferences(new Error('references unavailable'))

      await expect(opening).resolves.toBe(false)
      expect(useStore.getState().view).toBe('inbox')
      expect(useStore.getState().inboxQuery).toBe('status=unread')
      expect(useStore.getState().canvasTransformReferences).toBe(ownedReferences)
      expect(localStorage.getItem('dp-open-alice')).not.toBe('deferred-references')
    },
  )

  it('isolates cached roles by user and fails closed across an identity change', async () => {
    const doc = { id: 'shared', version: 1, name: 'shared', nodes: [], edges: [] }
    useStore.getState().loadDoc(doc, 'owner')
    expect(localStorage.getItem('dp-canvas-role-alice-shared')).toBeNull() // local state alone is not authority
    apiMocks.listCanvases.mockResolvedValue([{ id: 'shared', name: 'shared', version: 1, role: 'owner' }])
    await useStore.getState().refreshFiles() // only this authoritative response is cached
    expect(localStorage.getItem('dp-canvas-role-alice-shared')).toBe('owner')

    // Bob must not inherit Alice's owner role during the user-switch/startup window.
    useStore.setState({ currentUser: { id: 'bob', name: 'Bob' } })
    expect(useStore.getState().canvasRole).toBeNull()
    useStore.getState().loadDoc(doc) // unknown Bob role stays fail-closed
    expect(useStore.getState().canvasRole).toBeNull()
    expect(useStore.getState().addNode('source', { x: 0, y: 0 })).toBeNull()
    expect(localStorage.getItem('dp-canvas-role-bob-shared')).toBeNull()

    // Once Bob's own server response says viewer, only Bob's cache receives that role.
    apiMocks.listCanvases.mockResolvedValue([{ id: 'shared', name: 'shared', version: 1, role: 'viewer' }])
    await useStore.getState().refreshFiles()
    expect(useStore.getState().canvasRole).toBe('viewer')
    expect(localStorage.getItem('dp-canvas-role-bob-shared')).toBe('viewer')
    expect(localStorage.getItem('dp-canvas-role-alice-shared')).toBe('owner')
  })

  it('fails closed immediately when an authoritative file refresh no longer includes the open canvas', async () => {
    const doc = { id: 'shared', version: 1, name: 'shared', nodes: [], edges: [] }
    useStore.getState().loadDoc(doc, 'owner')
    apiMocks.listCanvases.mockResolvedValue([{ id: 'shared', name: 'shared', version: 1, role: 'owner' }])
    await useStore.getState().refreshFiles()
    useStore.setState({ agentOpen: true })
    expect(localStorage.getItem('dp-canvas-role-alice-shared')).toBe('owner')

    apiMocks.listCanvases.mockResolvedValue([]) // revoked or deleted on the server
    await useStore.getState().refreshFiles()

    expect(useStore.getState().canvasRole).toBeNull()
    expect(useStore.getState().agentOpen).toBe(false)
    expect(localStorage.getItem('dp-canvas-role-alice-shared')).toBeNull()
    expect(useStore.getState().addNode('source', { x: 0, y: 0 })).toBeNull()
  })

  it('does not treat a failed file-list refresh as an authoritative revocation', async () => {
    const doc = emptyTestDoc('shared')
    useStore.getState().loadDoc(doc, 'owner')
    apiMocks.listCanvases.mockResolvedValueOnce([{ id: 'shared', name: 'shared', version: 1, role: 'owner' }])
    expect(await useStore.getState().refreshFiles()).toBe(true)
    expect(localStorage.getItem('dp-canvas-role-alice-shared')).toBe('owner')

    apiMocks.listCanvases.mockRejectedValueOnce(new TypeError('offline'))
    expect(await useStore.getState().refreshFiles()).toBe(false)

    expect(useStore.getState().canvasRole).toBe('owner')
    expect(localStorage.getItem('dp-canvas-role-alice-shared')).toBe('owner')
  })

  it('shows a visible error and keeps local authority when Canvas deletion fails', async () => {
    useStore.setState({ view: 'canvas', agentOpen: true })
    apiMocks.deleteCanvas.mockRejectedValueOnce(new TypeError('hub offline'))

    await useStore.getState().deleteFile('c')

    expect(useStore.getState()).toMatchObject({
      view: 'canvas', canvasRole: 'owner', agentOpen: true,
      files: [{ id: 'c', role: 'owner' }],
    })
    expect(useStore.getState().toasts).toContainEqual(expect.objectContaining({
      kind: 'error', msg: 'Could not delete the Canvas: hub offline',
    }))
  })

  it('revokes a deleted Canvas immediately and never reopens it when list refresh fails', async () => {
    useStore.setState({ view: 'canvas', agentOpen: true })
    localStorage.setItem('dp-canvas-role-alice-c', 'owner')
    let rejectRefresh!: (error: Error) => void
    apiMocks.listCanvases.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      rejectRefresh = reject
    }))

    const deleting = useStore.getState().deleteFile('c')
    await vi.waitFor(() => expect(apiMocks.listCanvases).toHaveBeenCalledOnce())

    expect(useStore.getState()).toMatchObject({
      view: 'canvas', canvasRole: null, agentOpen: false, files: [],
    })
    expect(localStorage.getItem('dp-canvas-role-alice-c')).toBeNull()

    rejectRefresh(new TypeError('list offline'))
    await deleting

    expect(apiMocks.getCanvas).not.toHaveBeenCalledWith('c')
    expect(useStore.getState()).toMatchObject({
      view: 'workspace', canvasRole: null, files: [], firstRunChoice: false,
    })
  })

  it('opens fail-closed when the document loads but its fresh role cannot be confirmed', async () => {
    const doc = emptyTestDoc('shared')
    useStore.getState().loadDoc(doc, 'owner')
    apiMocks.listCanvases.mockResolvedValueOnce([{ id: 'shared', name: 'shared', version: 1, role: 'owner' }])
    await useStore.getState().refreshFiles()
    expect(localStorage.getItem('dp-canvas-role-alice-shared')).toBe('owner')

    apiMocks.getCanvas.mockResolvedValue(doc)
    apiMocks.listCanvases.mockRejectedValueOnce(new TypeError('offline'))
    expect(await useStore.getState().openFile('shared')).toBe(true)

    expect(useStore.getState().canvasRole).toBeNull()
    // The network failure was not a revocation: keep the last confirmed cache for offline bootstrap.
    expect(localStorage.getItem('dp-canvas-role-alice-shared')).toBe('owner')
    expect(useStore.getState().toasts.some((toast) => toast.msg.includes('Opened read-only'))).toBe(true)
  })

  it('surfaces an explicit read-only message when reopen confirms access was removed', async () => {
    const doc = emptyTestDoc('shared')
    apiMocks.getCanvas.mockResolvedValue(doc)
    useStore.setState({ files: [{ id: 'shared', name: 'shared', version: 1, role: 'owner' }] })
    apiMocks.listCanvases.mockResolvedValue([])

    expect(await useStore.getState().openFile('shared')).toBe(true)

    expect(useStore.getState().canvasRole).toBeNull()
    expect(useStore.getState().toasts.some((toast) => toast.msg.includes('no longer in your accessible files'))).toBe(true)
  })

  it('preserves the current canvas when new-file or example creation is forbidden', async () => {
    const before = useStore.getState().doc
    const beforePast = [emptyTestDoc('undo')]
    useStore.setState({ past: beforePast, saved: false })
    apiMocks.createCanvas.mockRejectedValue(new KernelError(403, 'forbidden'))

    expect(await useStore.getState().newFile()).toEqual({ ok: false })
    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().canvasRole).toBe('owner')
    expect(useStore.getState().past).toBe(beforePast)
    expect(useStore.getState().saved).toBe(false)

    expect(await useStore.getState().newFromExample('purchases')).toEqual({ ok: false })
    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().canvasRole).toBe('owner')
    expect(useStore.getState().toasts.filter((toast) => toast.msg.includes('permission'))).toHaveLength(2)
  })

  it('replaces an explicit pristine blank with an example in place', async () => {
    const blank = emptyTestDoc('pristine')
    blank.name = 'untitled'
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({
      serverVersion: 1,
      currentDraftId: null,
      files: [{ id: blank.id, name: blank.name, version: 1, role: 'owner' }],
    })
    apiMocks.listRuns.mockResolvedValue([])
    apiMocks.saveCanvas.mockResolvedValue({ ok: true, id: 'pristine', version: 2 })

    expect(await useStore.getState().newFromExample('purchases', 'replace-pristine')).toMatchObject({ ok: true, canvasId: 'pristine' })
    expect(apiMocks.createCanvas).not.toHaveBeenCalled()
    expect(apiMocks.saveCanvas).toHaveBeenCalledWith(expect.objectContaining({ id: 'pristine' }), false, 1)
    expect(useStore.getState().doc.nodes.length).toBeGreaterThan(0)
    const fitRequest = useStore.getState().viewportFitRequest
    expect(fitRequest).toMatchObject({ canvasId: 'pristine' })
    expect(fitRequest?.documentIdentity).toContain('pristine')
    useStore.getState().acknowledgeViewportFit(fitRequest!.id)
    expect(useStore.getState().viewportFitRequest).toBeNull()
    useStore.setState({ saved: false }) // an ordinary rerender must not manufacture another request
    expect(useStore.getState().viewportFitRequest).toBeNull()
  })

  it.each(['purchases', 'top3', 'quality'])(
    'persists and reopens the %s example with its canonical local Source identity',
    async (key) => {
      const table: CatalogTable = {
        id: 'tbl-events', registrationId: 'registration-events', name: 'events',
        uri: '/workspace/data/events.parquet', rowCount: 2000, columns: [],
      }
      let persisted: CanvasDoc | null = null
      apiMocks.resolveExampleSources.mockResolvedValueOnce({
        resolutions: [{ ref: 'events', state: 'resolved', table }],
      })
      apiMocks.createCanvas.mockImplementationOnce(async (doc: CanvasDoc) => {
        persisted = structuredClone(doc)
        return { ok: true, id: doc.id, created: true }
      })

      const created = await useStore.getState().newFromExample(key)

      expect(created).toMatchObject({ ok: true, persistence: 'remote' })
      const persistedSource = persisted?.nodes.find((node) => node.type === 'source')
      expect(persistedSource?.data.config).toMatchObject({
        uri: table.uri, tableId: table.id, registrationId: table.registrationId,
      })

      apiMocks.getCanvas.mockResolvedValueOnce(structuredClone(persisted!))
      apiMocks.listCanvases.mockResolvedValueOnce([{
        id: persisted!.id, name: persisted!.name, version: 1, role: 'owner',
      }])
      expect(await useStore.getState().openFile(persisted!.id, { serverCopy: true })).toBe(true)
      expect(useStore.getState().doc.nodes.find((node) => node.type === 'source')?.data.config)
        .toMatchObject({ uri: table.uri, tableId: table.id, registrationId: table.registrationId })
    },
  )

  it('prefers an exact registered URI over a same-name Catalog candidate', async () => {
    const exact: CatalogTable = {
      id: 'tbl-exact', registrationId: 'registration-exact', name: 'seeded-events',
      uri: 'events', rowCount: 2000, columns: [],
    }
    let persisted: CanvasDoc | null = null
    apiMocks.resolveExampleSources.mockResolvedValueOnce({
      resolutions: [{ ref: 'events', state: 'resolved', table: exact }],
    })
    apiMocks.createCanvas.mockImplementationOnce(async (doc: CanvasDoc) => {
      persisted = structuredClone(doc)
      return { ok: true, id: doc.id, created: true }
    })

    expect(await useStore.getState().newFromExample('purchases')).toMatchObject({ ok: true })
    expect(persisted?.nodes.find((node) => node.type === 'source')?.data.config).toMatchObject({
      uri: exact.uri, tableId: exact.id, registrationId: exact.registrationId,
    })
  })

  it('keeps the bare example Source when its Catalog name is ambiguous', async () => {
    let persisted: CanvasDoc | null = null
    apiMocks.resolveExampleSources.mockResolvedValueOnce({
      resolutions: [{ ref: 'events', state: 'ambiguous', table: null }],
    })
    apiMocks.createCanvas.mockImplementationOnce(async (doc: CanvasDoc) => {
      persisted = structuredClone(doc)
      return { ok: true, id: doc.id, created: true }
    })

    expect(await useStore.getState().newFromExample('purchases')).toMatchObject({ ok: true })
    expect(persisted?.nodes.find((node) => node.type === 'source')?.data.config)
      .toEqual({ uri: 'events' })
  })

  it('keeps the runnable bare Source URI when Catalog identity is unavailable offline', async () => {
    apiMocks.resolveExampleSources.mockRejectedValueOnce(new TypeError('offline'))
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))

    expect(await useStore.getState().newFromExample('purchases')).toMatchObject({
      ok: true, persistence: 'local-draft',
    })

    const source = useStore.getState().doc.nodes.find((node) => node.type === 'source')
    expect(source?.data.config).toEqual({ uri: 'events' })
  })

  it.each(['transport', '5xx'] as const)(
    'keeps separate-example folder placement through a %s create failure and retry',
    async (failure) => {
      apiMocks.createCanvas.mockRejectedValueOnce(failure === 'transport'
        ? new TypeError('response lost')
        : new KernelError(502, 'proxy lost the hub response'))

      const created = await useStore.getState().newFromExample('purchases', 'create-separate')
      expect(created).toMatchObject({ ok: true, persistence: 'local-draft' })
      if (!created.ok) throw new Error('expected a local example draft')
      const draft = useStore.getState().localDrafts.find((item) => item.draftId === created.canvasId)!
      expect(draft).toMatchObject({ besideCanvasId: 'c', createAttemptDoc: draft.doc })

      apiMocks.createCanvas.mockResolvedValueOnce({ ok: true, id: draft.canvasId, created: true })
      await useStore.getState().retryLocalDraft(draft.draftId)

      expect(apiMocks.createCanvas.mock.calls[1]).toEqual([
        draft.doc,
        { besideCanvasId: 'c' },
      ])
      expect(useStore.getState().localDrafts).toEqual([])
    },
  )

  it('reuses separate-example placement when a committed create response was lost', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('response lost'))
    const created = await useStore.getState().newFromExample('purchases', 'create-separate')
    if (!created.ok) throw new Error('expected a local example draft')
    const draft = useStore.getState().localDrafts.find((item) => item.draftId === created.canvasId)!
    apiMocks.createCanvas.mockResolvedValueOnce({ ok: true, id: draft.canvasId, created: false })
    apiMocks.listCanvases.mockResolvedValueOnce([{
      id: draft.canvasId, name: draft.name, version: 1, role: 'owner',
    }])
    apiMocks.getCanvas.mockResolvedValueOnce(draft.createAttemptDoc)

    await useStore.getState().retryLocalDraft(draft.draftId)

    expect(apiMocks.createCanvas.mock.calls[1]).toEqual([
      draft.doc,
      { besideCanvasId: 'c' },
    ])
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect(useStore.getState().localDrafts).toEqual([])
  })

  it('keeps an edit made while example Source identity is resolving', async () => {
    const blank = emptyTestDoc('resolve-wait-blank')
    blank.name = 'untitled'
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({ serverVersion: 1, currentDraftId: null })
    apiMocks.listRuns.mockResolvedValue([])
    let finishResolution!: (result: { resolutions: never[] }) => void
    apiMocks.resolveExampleSources.mockReturnValue(new Promise((resolve) => {
      finishResolution = resolve
    }))

    const creation = useStore.getState().newFromExample('purchases', 'replace-pristine')
    await vi.waitFor(() => expect(apiMocks.resolveExampleSources).toHaveBeenCalledOnce())
    useStore.getState().setRequirements(['duckdb>=1'])
    finishResolution({ resolutions: [] })

    expect(await creation).toEqual({ ok: false })
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect(useStore.getState().doc.requirements).toEqual(['duckdb>=1'])
    expect(useStore.getState().toasts.at(-1)?.msg).toContain('your edit was kept')
  })

  it('creates a separate example when an otherwise blank Canvas has run history', async () => {
    const blank = emptyTestDoc('ran-blank')
    blank.name = 'untitled'
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({
      serverVersion: 1,
      currentDraftId: null,
      files: [{ id: blank.id, name: blank.name, version: 1, role: 'owner' }],
    })
    apiMocks.listRuns.mockResolvedValue([{ runId: 'durable-user-work' }])

    expect(await useStore.getState().newFromExample('purchases', 'replace-pristine')).toMatchObject({ ok: true })
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect(apiMocks.createCanvas).toHaveBeenCalledOnce()
    expect((apiMocks.createCanvas.mock.calls[0][0] as { id: string }).id).not.toBe(blank.id)
    expect(apiMocks.createCanvas.mock.calls[0][1]).toEqual({ besideCanvasId: blank.id })
  })

  it('keeps an in-place example replacement as a version-fenced draft when its save response is lost', async () => {
    const blank = emptyTestDoc('response-loss-blank')
    blank.name = 'untitled'
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({ serverVersion: 7, currentDraftId: null })
    apiMocks.listRuns.mockResolvedValue([])
    apiMocks.saveCanvas.mockRejectedValueOnce(new TypeError('response lost'))

    expect(await useStore.getState().newFromExample('purchases', 'replace-pristine')).toMatchObject({
      ok: true, canvasId: blank.id, persistence: 'local-draft',
    })
    expect(useStore.getState().serverVersion).toBe(7)
    expect(useStore.getState().localDrafts).toMatchObject([{
      canvasId: blank.id,
      baseCanvasId: blank.id,
      baseVersion: 7,
      createAttemptDoc: null,
      syncState: 'dirty',
    }])
  })

  it('never upgrades a displayed separate-create action into an in-place replacement', async () => {
    const blank = emptyTestDoc('displayed-separate')
    blank.name = 'untitled'
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({ serverVersion: 1, currentDraftId: null })

    const result = await useStore.getState().newFromExample('purchases', 'create-separate')

    expect(result).toMatchObject({ ok: true })
    expect(apiMocks.listRuns).not.toHaveBeenCalled()
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect(apiMocks.createCanvas).toHaveBeenCalledOnce()
    expect((apiMocks.createCanvas.mock.calls[0][0] as { id: string }).id).not.toBe(blank.id)
  })

  it('preserves requirements on an otherwise empty Canvas by creating the example separately', async () => {
    const blank = { ...emptyTestDoc('configured-requirements'), name: 'untitled', requirements: ['polars>=1'] }
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({ serverVersion: 1, currentDraftId: null })

    const result = await useStore.getState().newFromExample('purchases', 'replace-pristine')

    expect(result).toMatchObject({ ok: true })
    expect(apiMocks.listRuns).not.toHaveBeenCalled()
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect((apiMocks.createCanvas.mock.calls[0][0] as { id: string }).id).not.toBe(blank.id)
    expect(blank.requirements).toEqual(['polars>=1'])
  })

  it('preserves parameters on an otherwise empty Canvas by creating the example separately', async () => {
    const blank = {
      ...emptyTestDoc('configured-parameters'),
      name: 'untitled',
      parameters: [{ name: 'threshold', type: 'float' as const, default: 0.5 }],
    }
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({ serverVersion: 1, currentDraftId: null })

    const result = await useStore.getState().newFromExample('purchases', 'replace-pristine')

    expect(result).toMatchObject({ ok: true })
    expect(apiMocks.listRuns).not.toHaveBeenCalled()
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect((apiMocks.createCanvas.mock.calls[0][0] as { id: string }).id).not.toBe(blank.id)
    expect(blank.parameters).toEqual([{ name: 'threshold', type: 'float', default: 0.5 }])
  })

  it('cancels an intended replacement when requirements or parameters change while run history is loading', async () => {
    const blank = emptyTestDoc('edited-during-history')
    blank.name = 'untitled'
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({ serverVersion: 1, currentDraftId: null })
    let finishRuns!: (runs: unknown[]) => void
    apiMocks.listRuns.mockReturnValue(new Promise((resolve) => { finishRuns = resolve }))

    const creation = useStore.getState().newFromExample('purchases', 'replace-pristine')
    useStore.getState().setRequirements(['duckdb>=1'])
    expect(useStore.getState().setParameters([
      { name: 'limit', type: 'integer', default: 100 },
    ])).toBeNull()
    const edited = useStore.getState().doc
    finishRuns([])

    expect(await creation).toEqual({ ok: false })
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect(apiMocks.createCanvas).not.toHaveBeenCalled()
    expect(useStore.getState().doc).toBe(edited)
    expect(edited.requirements).toEqual(['duckdb>=1'])
    expect(edited.parameters).toEqual([{ name: 'limit', type: 'integer', default: 100 }])
    expect(useStore.getState().toasts.at(-1)?.msg).toContain('your edit was kept')
  })

  it('keeps an added source mounted when an in-place example check resolves late', async () => {
    register({
      kind: 'deferred-example-source', title: 'source', category: 'io', inputs: [],
      outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false, blurb: '',
      defaultData: () => ({ title: 'source', status: 'draft', config: {} }),
    }, () => null)
    const blank = emptyTestDoc('node-edited-during-history')
    blank.name = 'untitled'
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({ serverVersion: 1, currentDraftId: null })
    let finishRuns!: (runs: unknown[]) => void
    apiMocks.listRuns.mockReturnValue(new Promise((resolve) => { finishRuns = resolve }))

    const creation = useStore.getState().newFromExample('purchases', 'replace-pristine')
    const source = useStore.getState().addNode('deferred-example-source', { x: 20, y: 40 })
    const edited = useStore.getState().doc
    finishRuns([])

    expect(await creation).toEqual({ ok: false })
    expect(source).not.toBeNull()
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect(apiMocks.createCanvas).not.toHaveBeenCalled()
    expect(useStore.getState().doc).toBe(edited)
    expect(useStore.getState().doc.id).toBe(blank.id)
    expect(useStore.getState().doc.nodes.map((node) => node.id)).toContain(source!.id)
    expect(useStore.getState().viewportFitRequest).toBeNull()
    expect(useStore.getState().toasts.at(-1)?.msg).toContain('Choose the example again')
  })

  it('downgrades an intended replacement when run history cannot be confirmed', async () => {
    const blank = emptyTestDoc('history-unavailable')
    blank.name = 'untitled'
    useStore.getState().loadDoc(blank, 'owner')
    useStore.setState({ serverVersion: 1, currentDraftId: null })
    apiMocks.listRuns.mockRejectedValue(new TypeError('offline'))

    expect(await useStore.getState().newFromExample('purchases', 'replace-pristine')).toMatchObject({ ok: true })
    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect(apiMocks.createCanvas).toHaveBeenCalledOnce()
    expect((apiMocks.createCanvas.mock.calls[0][0] as { id: string }).id).not.toBe(blank.id)
  })

  it('fails the current canvas closed when new-file creation returns 401', async () => {
    apiMocks.listCanvases.mockResolvedValue([{ id: 'c', name: 'test', version: 1, role: 'owner' }])
    await useStore.getState().refreshFiles()
    useStore.getState().setAgentOpen(true)
    apiMocks.createCanvas.mockRejectedValueOnce(new KernelError(401, 'session expired'))
    const before = useStore.getState().doc

    expect(await useStore.getState().newFile()).toEqual({ ok: false })

    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().canvasRole).toBeNull()
    expect(useStore.getState().agentOpen).toBe(false)
    expect(localStorage.getItem('dp-canvas-role-alice-c')).toBeNull()
    expect(useStore.getState().toasts.some((toast) => toast.msg.includes('session'))).toBe(true)
  })

  it('keeps local-first owner drafts for genuine transport failures', async () => {
    const beforeId = useStore.getState().doc.id
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))

    const created = await useStore.getState().newFile()

    expect(useStore.getState().doc.id).not.toBe(beforeId)
    expect(useStore.getState().canvasRole).toBe('owner')
    expect(useStore.getState().view).toBe('canvas')
    expect(created).toMatchObject({ ok: true, persistence: 'local-draft' })
    expect(apiMocks.createCanvas.mock.calls[0][1]).toEqual({ besideCanvasId: beforeId })
    expect(useStore.getState().localDrafts).toMatchObject([{
      canvasId: useStore.getState().doc.id,
      baseVersion: null,
      besideCanvasId: beforeId,
      syncState: 'dirty',
    }])
  })

  it('keeps a local-first owner draft when a 5xx leaves create outcome unknown', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new KernelError(502, 'proxy lost the hub response'))

    const created = await useStore.getState().newFile()

    expect(created).toMatchObject({ ok: true, persistence: 'local-draft' })
    expect(useStore.getState().localDrafts).toMatchObject([{
      canvasId: useStore.getState().doc.id,
      createAttemptDoc: useStore.getState().doc,
      baseVersion: null,
      besideCanvasId: 'c',
      syncState: 'dirty',
    }])
  })

  it('retries a local-only draft with the same stable idempotent create identity', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('response lost'))
    const created = await useStore.getState().newFile()
    if (!created.ok) throw new Error('expected local draft')
    const draft = useStore.getState().localDrafts[0]
    apiMocks.createCanvas.mockResolvedValueOnce({ ok: true, id: draft.canvasId, created: true })
    useStore.setState({ toasts: [] })

    await useStore.getState().retryLocalDraft(draft.draftId)

    expect(apiMocks.createCanvas).toHaveBeenLastCalledWith(draft.doc, { besideCanvasId: 'c' })
    expect(useStore.getState().localDrafts).toEqual([])
    expect(useStore.getState().currentDraftId).toBeNull()
    expect(useStore.getState().serverVersion).toBe(1)
    expect(useStore.getState().toasts.some((toast) => toast.msg === `Synced “${draft.name}”`)).toBe(true)
  })

  it('recovers a lost create response only after confirming owner and exact first content', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('response lost'))
    await useStore.getState().newFile()
    const draft = useStore.getState().localDrafts[0]
    const edited = { ...draft, doc: { ...draft.doc, name: 'edited offline' }, name: 'edited offline' }
    useStore.setState({ doc: edited.doc, localDrafts: [edited] })
    apiMocks.createCanvas.mockResolvedValueOnce({ ok: true, id: draft.canvasId, created: false })
    apiMocks.listCanvases.mockResolvedValueOnce([{ id: draft.canvasId, name: 'untitled', version: 1, role: 'owner' }])
    apiMocks.getCanvas.mockResolvedValueOnce(draft.createAttemptDoc)
    apiMocks.saveCanvas.mockResolvedValueOnce({ ok: true, id: draft.canvasId, version: 2 })

    await useStore.getState().retryLocalDraft(draft.draftId)

    expect(apiMocks.createCanvas.mock.calls[1][1]).toEqual({ besideCanvasId: 'c' })
    expect(apiMocks.saveCanvas).toHaveBeenCalledWith(edited.doc, false, 1)
    expect(useStore.getState().localDrafts).toEqual([])
    expect(useStore.getState().doc.version).toBe(2)
  })

  it('marks a same-id create collision as a conflict instead of overwriting it', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))
    await useStore.getState().newFile()
    const draft = useStore.getState().localDrafts[0]
    apiMocks.createCanvas.mockResolvedValueOnce({ ok: true, id: draft.canvasId, created: false })
    apiMocks.listCanvases.mockResolvedValueOnce([{ id: draft.canvasId, name: 'other', version: 3, role: 'owner' }])
    apiMocks.getCanvas.mockResolvedValueOnce({ ...draft.doc, name: 'other', version: 3 })

    await useStore.getState().retryLocalDraft(draft.draftId)

    expect(apiMocks.saveCanvas).not.toHaveBeenCalled()
    expect(useStore.getState().localDrafts[0]).toMatchObject({ syncState: 'conflict' })
    expect(useStore.getState().localDrafts[0].lastError).toContain('changed or was deleted')
  })

  it('preserves an existing-canvas draft when the server canvas was deleted', async () => {
    const doc = { ...emptyTestDoc('existing'), name: 'edited offline' }
    expect(writeCanvasDraft({
      draftId: doc.id,
      principalId: 'alice',
      canvasId: doc.id,
      baseCanvasId: doc.id,
      baseVersion: 1,
      name: doc.name,
      doc,
      createAttemptDoc: null,
      syncState: 'dirty',
      lastLocalEditAt: '2026-07-18T12:00:00.000Z',
    }).ok).toBe(true)
    useStore.getState().refreshLocalDrafts()
    expect(useStore.getState().openLocalDraft(doc.id)).toBe(true)
    apiMocks.saveCanvas.mockRejectedValueOnce(new KernelError(409, 'canvas was deleted'))

    await useStore.getState().retryLocalDraft(doc.id)

    expect(apiMocks.saveCanvas).toHaveBeenCalledWith(doc, false, 1)
    expect(apiMocks.createCanvas).not.toHaveBeenCalled()
    expect(useStore.getState().localDrafts[0]).toMatchObject({
      syncState: 'conflict',
      doc,
    })
  })

  it.each([
    ['New Canvas', () => useStore.getState().newFile()],
    ['example Canvas', () => useStore.getState().newFromExample('purchases', 'create-separate')],
  ])('creates a %s at the Workspace root from a draft whose server base is unavailable', async (_label, create) => {
    const doc = { ...emptyTestDoc('unavailable-base'), name: 'recovered work' }
    expect(writeCanvasDraft({
      draftId: doc.id,
      principalId: 'alice',
      canvasId: doc.id,
      baseCanvasId: doc.id,
      baseVersion: 1,
      name: doc.name,
      doc,
      createAttemptDoc: null,
      syncState: 'dirty',
      lastLocalEditAt: '2026-07-18T12:00:00.000Z',
    }).ok).toBe(true)
    useStore.getState().refreshLocalDrafts()
    useStore.setState({ files: [] })
    expect(useStore.getState().openLocalDraft(doc.id)).toBe(true)

    expect(await create()).toMatchObject({ ok: true, persistence: 'remote' })

    expect(apiMocks.createCanvas).toHaveBeenCalledOnce()
    expect(apiMocks.createCanvas.mock.calls[0]).toHaveLength(1)
  })

  it('offers one actionable recovery notification for a concurrent Canvas edit', async () => {
    const doc = { ...emptyTestDoc('existing'), name: 'local edit', nodes: [NODE('local-node')] }
    expect(writeCanvasDraft({
      draftId: doc.id,
      principalId: 'alice',
      canvasId: doc.id,
      baseCanvasId: doc.id,
      baseVersion: 1,
      name: doc.name,
      doc,
      createAttemptDoc: null,
      syncState: 'dirty',
      lastLocalEditAt: '2026-07-25T12:00:00.000Z',
    }).ok).toBe(true)
    useStore.getState().refreshLocalDrafts()
    expect(useStore.getState().openLocalDraft(doc.id)).toBe(true)
    apiMocks.saveCanvas.mockRejectedValue(new KernelError(409, 'another session saved first'))

    await useStore.getState().retryLocalDraft(doc.id)
    await useStore.getState().retryLocalDraft(doc.id)

    const conflicts = useStore.getState().toasts.filter((toast) => toast.dedupeKey === `canvas-sync-conflict:${doc.id}`)
    expect(conflicts).toHaveLength(1)
    const conflict = conflicts[0]
    expect(conflict.msg).toContain('local draft is preserved')
    expect(conflict.sticky).toBe(true)
    expect(conflict.actions?.map((action) => action.label)).toEqual([
      'Open server copy',
      'Keep local draft as new Canvas',
    ])

    useStore.getState().dismissToast(conflict.id)
    useStore.getState().notifyLocalDraftConflict(doc.id)
    expect(useStore.getState().toasts.filter((toast) => (
      toast.dedupeKey === `canvas-sync-conflict:${doc.id}`
    ))).toMatchObject([{ sticky: true, msg: conflict.msg }])

    const serverCopy = { ...doc, name: 'server edit', version: 2 }
    apiMocks.getCanvas.mockResolvedValueOnce(serverCopy)
    await conflict.actions?.[0].onClick()
    expect(apiMocks.getCanvas).toHaveBeenCalledWith(doc.id)
    expect(useStore.getState().doc).toMatchObject({ id: doc.id, name: 'server edit', version: 2 })

    await conflict.actions?.[1].onClick()
    expect(apiMocks.createCanvas).toHaveBeenLastCalledWith(expect.objectContaining({
      id: expect.not.stringMatching(new RegExp(`^${doc.id}$`)),
      name: 'local edit (recovered)',
      nodes: doc.nodes,
    }))
    expect(useStore.getState().doc).toMatchObject({ name: 'local edit (recovered)', nodes: doc.nodes })
    expect(useStore.getState().localDrafts).toEqual([])
    expect(useStore.getState().toasts.filter((toast) => (
      toast.dedupeKey === `canvas-sync-conflict:${doc.id}`
    ))).toEqual([])
  })

  it('drops the conflict notification when the draft it points at is discarded', async () => {
    const doc = { ...emptyTestDoc('discarded'), name: 'local edit', nodes: [NODE('local-node')] }
    expect(writeCanvasDraft({
      draftId: doc.id,
      principalId: 'alice',
      canvasId: doc.id,
      baseCanvasId: doc.id,
      baseVersion: 1,
      name: doc.name,
      doc,
      createAttemptDoc: null,
      syncState: 'dirty',
      lastLocalEditAt: '2026-07-25T12:00:00.000Z',
    }).ok).toBe(true)
    useStore.getState().refreshLocalDrafts()
    expect(useStore.getState().openLocalDraft(doc.id)).toBe(true)
    apiMocks.saveCanvas.mockRejectedValue(new KernelError(409, 'another session saved first'))
    await useStore.getState().retryLocalDraft(doc.id)
    expect(useStore.getState().toasts.some((toast) => (
      toast.dedupeKey === `canvas-sync-conflict:${doc.id}`
    ))).toBe(true)

    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(true)
    await useStore.getState().discardLocalDraft(doc.id)
    confirm.mockRestore()

    expect(useStore.getState().toasts.filter((toast) => (
      toast.dedupeKey === `canvas-sync-conflict:${doc.id}`
    ))).toEqual([])
  })

  it('clears another principal draft list synchronously on identity change', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))
    await useStore.getState().newFile()
    expect(useStore.getState().localDrafts).toHaveLength(1)

    useStore.setState({ currentUser: { id: 'bob', name: 'Bob' } })

    expect(useStore.getState().localDrafts).toEqual([])
    expect(useStore.getState().currentDraftId).toBeNull()
    expect(useStore.getState().canvasRole).toBeNull()
  })

  it('deletes the selected local-only draft without deleting a server canvas', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))
    await useStore.getState().newFile()
    const draft = useStore.getState().localDrafts[0]

    await useStore.getState().discardLocalDraft(draft.draftId)

    expect(useStore.getState().localDrafts).toEqual([])
    expect(apiMocks.deleteCanvas).not.toHaveBeenCalled()
    expect(useStore.getState().doc.id).not.toBe(draft.canvasId)
  })

  it('creates a fresh Canvas when every stale fallback fails after discarding a draft', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))
    await useStore.getState().newFile()
    const draft = useStore.getState().localDrafts[0]
    apiMocks.getCanvas.mockRejectedValueOnce(new KernelError(404, 'gone'))
    apiMocks.listCanvases.mockResolvedValueOnce([])

    await useStore.getState().discardLocalDraft(draft.draftId)

    expect(apiMocks.getCanvas).toHaveBeenCalledWith('c')
    expect(apiMocks.createCanvas).toHaveBeenCalledTimes(2)
    expect(useStore.getState().doc.id).not.toBe(draft.canvasId)
    expect(useStore.getState().doc.id).not.toBe('c')
    expect(useStore.getState().currentDraftId).toBeNull()
  })

  it('does not let Alice\'s discarded draft fallback navigate or create for Bob', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))
    await useStore.getState().newFile()
    const draft = useStore.getState().localDrafts[0]
    let finishFallback!: (doc: CanvasDoc) => void
    apiMocks.getCanvas.mockImplementationOnce(() => new Promise((resolve) => { finishFallback = resolve }))

    const discarding = useStore.getState().discardLocalDraft(draft.draftId)
    await vi.waitFor(() => expect(apiMocks.getCanvas).toHaveBeenCalledWith('c'))
    useStore.setState({ currentUser: { id: 'bob', name: 'Bob' } })
    finishFallback(emptyTestDoc('c'))
    await discarding

    expect(useStore.getState().currentUser?.id).toBe('bob')
    expect(apiMocks.createCanvas).toHaveBeenCalledTimes(1)
    expect(useStore.getState().doc.id).toBe(draft.canvasId)
  })

  it('does not let a discarded draft fallback overwrite newer navigation', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))
    await useStore.getState().newFile()
    const draft = useStore.getState().localDrafts[0]
    let finishFallback!: (doc: CanvasDoc) => void
    apiMocks.getCanvas.mockImplementationOnce(() => new Promise((resolve) => { finishFallback = resolve }))

    const discarding = useStore.getState().discardLocalDraft(draft.draftId)
    await vi.waitFor(() => expect(apiMocks.getCanvas).toHaveBeenCalledWith('c'))
    useStore.getState().setJobsQuery('status=failed')
    finishFallback(emptyTestDoc('c'))
    await discarding

    expect(useStore.getState()).toMatchObject({ view: 'jobs', jobsQuery: 'status=failed' })
    expect(apiMocks.createCanvas).toHaveBeenCalledTimes(1)
    expect(useStore.getState().doc.id).toBe(draft.canvasId)
  })

  it('keeps a local draft until its in-flight sync has settled', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))
    await useStore.getState().newFile()
    const draft = useStore.getState().localDrafts[0]
    let finishRetry!: (result: { ok: boolean; id: string; created: boolean }) => void
    apiMocks.createCanvas.mockImplementationOnce(() => new Promise((resolve) => {
      finishRetry = resolve
    }))

    const retrying = useStore.getState().retryLocalDraft(draft.draftId)
    expect(useStore.getState().localDrafts[0]?.syncState).toBe('syncing')

    await useStore.getState().discardLocalDraft(draft.draftId)

    expect(useStore.getState().localDrafts).toContainEqual(expect.objectContaining({
      draftId: draft.draftId, syncState: 'syncing',
    }))
    expect(useStore.getState().toasts).toContainEqual(expect.objectContaining({
      kind: 'info', msg: 'Wait for this local draft to finish syncing before deleting it',
    }))

    finishRetry({ ok: true, id: draft.canvasId, created: true })
    await retrying
    expect(useStore.getState().localDrafts).toEqual([])
  })

  it('does not reintroduce a previous principal draft when an in-flight retry settles', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))
    await useStore.getState().newFile()
    const draft = useStore.getState().localDrafts[0]
    let rejectRetry!: (error: Error) => void
    apiMocks.createCanvas.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      rejectRetry = reject
    }))

    const retrying = useStore.getState().retryLocalDraft(draft.draftId)
    useStore.setState({ currentUser: { id: 'bob', name: 'Bob' } })
    rejectRetry(new TypeError('late response loss'))
    await retrying

    expect(useStore.getState().currentUser?.id).toBe('bob')
    expect(useStore.getState().localDrafts).toEqual([])
    expect(useStore.getState().toasts).toEqual([])
  })

  it('does not overwrite newer local edits when an in-flight retry fails', async () => {
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))
    await useStore.getState().newFile()
    const original = useStore.getState().localDrafts[0]
    let rejectRetry!: (error: Error) => void
    apiMocks.createCanvas.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      rejectRetry = reject
    }))

    const retrying = useStore.getState().retryLocalDraft(original.draftId)
    const editedDoc = { ...original.doc, name: 'edited while retrying' }
    useStore.setState((state) => ({
      doc: editedDoc,
      localDrafts: state.localDrafts.map((draft) => draft.draftId === original.draftId
        ? { ...draft, doc: editedDoc, name: editedDoc.name!, syncState: 'dirty' as const,
          lastLocalEditAt: '2026-07-18T13:00:00.000Z' }
        : draft),
    }))
    rejectRetry(new TypeError('late response loss'))
    await retrying

    expect(useStore.getState().localDrafts[0]).toMatchObject({
      doc: editedDoc,
      name: 'edited while retrying',
      syncState: 'dirty',
      lastLocalEditAt: '2026-07-18T13:00:00.000Z',
    })
  })

  it('keeps the importer destination as a local draft on a genuine transport failure', async () => {
    const beforeId = useStore.getState().doc.id
    const controller = new AbortController()
    apiMocks.createCanvas.mockRejectedValueOnce(new TypeError('offline'))

    const created = await useStore.getState().newFile({ signal: controller.signal })

    expect(created).toMatchObject({ ok: true, persistence: 'local-draft' })
    expect(useStore.getState().doc.id).not.toBe(beforeId)
    expect(useStore.getState().canvasRole).toBe('owner')
    expect(apiMocks.deleteCanvas).not.toHaveBeenCalled()
  })

  it('reports a remote canvas creation target and only applies an import to that target', async () => {
    const created = await useStore.getState().newFile()
    expect(created).toMatchObject({ ok: true, persistence: 'remote' })
    if (!created.ok) throw new Error('expected a canvas')

    expect(useStore.getState().applyAgentGraph({ nodes: [NODE('imported')], edges: [] }, created.canvasId)).toBe(true)
    expect(useStore.getState().doc.nodes.map((node) => node.id)).toEqual(['imported'])

    useStore.setState({ doc: emptyTestDoc('other'), view: 'canvas' })
    expect(useStore.getState().applyAgentGraph({ nodes: [NODE('must-not-apply')], edges: [] }, created.canvasId)).toBe(false)
    expect(useStore.getState().doc.nodes).toEqual([])
  })

  it('cancels a pending canvas creation when the researcher navigates away', async () => {
    let finishCreate!: (value: { ok: boolean; id: string; created: boolean }) => void
    apiMocks.createCanvas.mockImplementationOnce(() => new Promise((resolve) => { finishCreate = resolve }))
    const before = useStore.getState().doc

    const creating = useStore.getState().newFile()
    const pendingDoc = apiMocks.createCanvas.mock.calls[0][0] as { id: string }
    useStore.getState().setView('files')
    finishCreate({ ok: true, id: pendingDoc.id, created: true })

    expect(await creating).toEqual({ ok: false })
    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().view).toBe('files')
  })

  it('waits for confirmed insertion, cleans up a cancelled remote canvas, and never activates it', async () => {
    let finishCreate!: (value: { ok: boolean; id: string; created: boolean }) => void
    apiMocks.createCanvas.mockImplementationOnce(() => new Promise((resolve) => { finishCreate = resolve }))
    const controller = new AbortController()
    const before = useStore.getState().doc
    const beforeView = useStore.getState().view

    const creating = useStore.getState().newFile({ signal: controller.signal })
    const pendingDoc = apiMocks.createCanvas.mock.calls[0][0] as { id: string }
    expect(apiMocks.createCanvas.mock.calls[0]).toHaveLength(2)
    expect(apiMocks.createCanvas.mock.calls[0][1]).toEqual({ besideCanvasId: 'c' })
    controller.abort()
    finishCreate({ ok: true, id: pendingDoc.id, created: true })

    expect(await creating).toEqual({ ok: false })
    expect(apiMocks.deleteCanvas).toHaveBeenCalledWith(pendingDoc.id)
    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().view).toBe(beforeView)
    expect(useStore.getState().toasts).toEqual([])
  })

  it('retains a failed-cleanup remote draft without navigating or reporting import success', async () => {
    let finishCreate!: (value: { ok: boolean; id: string; created: boolean }) => void
    apiMocks.createCanvas.mockImplementationOnce(() => new Promise((resolve) => { finishCreate = resolve }))
    apiMocks.deleteCanvas.mockRejectedValueOnce(new TypeError('cleanup offline'))
    const controller = new AbortController()
    const before = useStore.getState().doc
    const beforeView = useStore.getState().view

    const creating = useStore.getState().newFile({ signal: controller.signal })
    const pendingDoc = apiMocks.createCanvas.mock.calls[0][0] as { id: string }
    controller.abort()
    finishCreate({ ok: true, id: pendingDoc.id, created: true })

    expect(await creating).toEqual({ ok: false })
    expect(apiMocks.deleteCanvas).toHaveBeenCalledWith(pendingDoc.id)
    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().view).toBe(beforeView)
    expect(useStore.getState().toasts).toEqual([])
  })

  it('never deletes or activates an existing canvas ID returned by create', async () => {
    apiMocks.createCanvas.mockImplementationOnce(async (doc: { id: string }) => (
      { ok: true, id: doc.id, created: false }
    ))
    const controller = new AbortController()
    const before = useStore.getState().doc
    const beforeView = useStore.getState().view

    expect(await useStore.getState().newFile({ signal: controller.signal })).toEqual({ ok: false })

    expect(apiMocks.deleteCanvas).not.toHaveBeenCalled()
    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().view).toBe(beforeView)
    expect(useStore.getState().toasts).toEqual([])
  })

  it('retains a possible remote draft when cancellation makes the create outcome unknown', async () => {
    let loseResponse!: (error: Error) => void
    apiMocks.createCanvas.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      loseResponse = reject
    }))
    const controller = new AbortController()
    const before = useStore.getState().doc
    const beforeView = useStore.getState().view

    const creating = useStore.getState().newFile({ signal: controller.signal })
    controller.abort()
    loseResponse(new TypeError('response lost after commit'))

    expect(await creating).toEqual({ ok: false })
    expect(apiMocks.deleteCanvas).not.toHaveBeenCalled()
    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().view).toBe(beforeView)
    expect(useStore.getState().toasts).toEqual([])
  })

  it('keeps typed bindings through estimate and run while fencing stale estimate responses', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/events.parquet' }
    const target = NODE('target', 'filter')
    target.data.config = { threshold: { parameterRef: 'threshold' } }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [],
      parameters: [{ name: 'threshold', type: 'integer' as const, required: true }],
      nodes: [source, target], edges: [{ id: 'edge', source: 'source', target: 'target' }] }
    useStore.setState({ doc, runs: {}, previewBindings: {} })
    apiMocks.estimate.mockResolvedValueOnce({ rows: 10, placement: 'local', needsConfirm: true })

    await useStore.getState().requestRun('target')
    useStore.getState().setRunParameterBinding('target', { name: 'threshold', value: 10 })
    await useStore.getState().submitRunParameters('target')

    expect(apiMocks.estimate).toHaveBeenCalledWith(
      doc, 'target', undefined, [{ name: 'threshold', value: 10 }])
    expect(useStore.getState().runs.target).toMatchObject({
      phase: 'confirm', parameterBindings: [{ name: 'threshold', value: 10 }],
    })

    let finish!: (value: unknown) => void
    apiMocks.estimate.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    useStore.getState().editRunParameters('target')
    const pending = useStore.getState().submitRunParameters('target')
    await vi.waitFor(() => expect(apiMocks.estimate).toHaveBeenCalledTimes(2))
    useStore.getState().setRunParameterBinding('target', { name: 'threshold', value: 11 })
    finish({ rows: 20, placement: 'local', needsConfirm: false })
    await pending
    expect(useStore.getState().runs.target).toMatchObject({
      phase: 'estimating', parametersReady: false,
      parameterBindings: [{ name: 'threshold', value: 11 }],
    })
  })

  it('opens the shared parameter gate before a panel-only estimate', async () => {
    const target = NODE('target', 'filter')
    target.data.config = { threshold: { parameterRef: 'threshold' } }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [],
      parameters: [{ name: 'threshold', type: 'integer' as const, required: true }],
      nodes: [target], edges: [] }
    useStore.setState({ doc, runs: {} })

    await useStore.getState().estimate('target')
    expect(useStore.getState().runs.target).toMatchObject({
      phase: 'parameters', parameterContinuation: { kind: 'estimate' },
    })
    expect(apiMocks.estimate).not.toHaveBeenCalled()

    useStore.getState().setRunParameterBinding('target', { name: 'threshold', value: 10 })
    await useStore.getState().submitRunParameters('target')
    expect(apiMocks.estimate).toHaveBeenCalledWith(
      doc, 'target', undefined, [{ name: 'threshold', value: 10 }])
    expect(apiMocks.run).not.toHaveBeenCalled()
  })

  it('passes one binding through preview, drift, run, and write admission identity', async () => {
    const source = NODE('source')
    source.data.config = { uri: '/data/events.parquet' }
    const target = NODE('target', 'filter')
    target.data.config = { threshold: { parameterRef: 'threshold' } }
    const write = NODE('write', 'write')
    write.data.config = { filename: { parameterRef: 'output' } }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [],
      parameters: [
        { name: 'threshold', type: 'integer' as const, required: true },
        { name: 'output', type: 'string' as const, required: true },
      ], nodes: [source, target, write], edges: [
        { id: 'source-target', source: 'source', target: 'target' },
        { id: 'target-write', source: 'target', target: 'write' },
      ] }
    const threshold = [{ name: 'threshold', value: 10 }]
    useStore.setState({ doc, runs: { target: { phase: 'idle', parameterBindings: threshold } }, previewBindings: {} })
    apiMocks.preview.mockResolvedValueOnce(previewResult('bound'))
    await useStore.getState().runPreview('target')
    expect(apiMocks.preview).toHaveBeenCalledWith(doc, 'target', 50, 0, undefined, undefined, threshold)

    const manifest = [{ node_id: 'source', dataset_id: 'dataset', revision_id: '1', provider: 'local', resolved_at: 'now' }]
    useStore.setState({ previewBindings: { target: {
      canvasId: 'c', nodeId: 'target', planIdentity: previewPlanIdentity(doc, 'target'),
      parameterBindings: threshold, inputManifest: manifest,
    } } })
    const failedRun = {
      runId: 'bound-run', status: 'failed', jobType: 'run', targetNodeId: 'target',
      rowsProcessed: 0, ms: 1, placement: 'local', perNode: [], outputs: [], error: 'expected',
    }
    apiMocks.run.mockResolvedValueOnce(failedRun)
    apiMocks.runStatus.mockResolvedValueOnce(failedRun)
    await useStore.getState().run('target')
    expect(apiMocks.inputDrift).toHaveBeenCalledWith(doc, 'target', manifest, threshold)
    expect(apiMocks.run).toHaveBeenCalledWith(
      doc, 'target', false, expect.any(String), manifest, undefined, threshold)

    const first = [{ name: 'output', value: 'one.parquet' }]
    useStore.setState({ runs: { write: { phase: 'idle', parameterBindings: first } } })
    apiMocks.writeAdmission.mockResolvedValue({ nodeId: 'write', managed: true, destination: '/out', mode: 'create', provider: 'local' })
    await useStore.getState().prepareWrite('write')
    useStore.getState().setRunParameterBinding('write', { name: 'output', value: 'two.parquet' })
    await useStore.getState().prepareWrite('write')
    expect(apiMocks.writeAdmission).toHaveBeenNthCalledWith(
      1, expect.objectContaining({ id: doc.id }), 'write', expect.any(String), undefined, first)
    expect(apiMocks.writeAdmission).toHaveBeenNthCalledWith(
      2, expect.objectContaining({ id: doc.id }), 'write', expect.any(String), undefined, [{ name: 'output', value: 'two.parquet' }])
  })

  it('uses the shared RunPanel binding gate before profile preflight without starting a job', async () => {
    const source = NODE('source')
    source.data.config = { uri: { parameterRef: 'source_uri' } }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [],
      parameters: [{ name: 'source_uri', type: 'string' as const, required: true }],
      nodes: [source], edges: [] }
    useStore.setState({ doc, runs: {}, profileJobs: {} })

    await useStore.getState().prepareFullProfile('source')
    expect(useStore.getState().runs.source).toMatchObject({
      phase: 'parameters', parameterContinuation: { kind: 'profile' },
    })
    expect(apiMocks.profileEstimate).not.toHaveBeenCalled()

    useStore.getState().setRunParameterBinding('source', { name: 'source_uri', value: '/data/events.parquet' })
    await useStore.getState().submitRunParameters('source')
    expect(apiMocks.profileEstimate).toHaveBeenCalledWith(
      doc, 'source', 'out', undefined,
      [{ name: 'source_uri', value: '/data/events.parquet' }])
    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'preflight', parameterBindings: [{ name: 'source_uri', value: '/data/events.parquet' }],
    })
    expect(apiMocks.fullProfile).not.toHaveBeenCalled()
  })

  it('treats every Play as a fresh one-shot parameter authorization and preserves prior values', async () => {
    const target = NODE('target', 'filter')
    target.data.config = { threshold: { parameterRef: 'threshold' } }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [],
      parameters: [{ name: 'threshold', type: 'integer' as const, required: true }],
      nodes: [target], edges: [] }
    useStore.setState({ doc, runs: {}, previewBindings: {} })
    apiMocks.estimate.mockResolvedValue({ rows: 10, placement: 'local', needsConfirm: true })

    await useStore.getState().requestRun('target')
    useStore.getState().setRunParameterBinding('target', { name: 'threshold', value: 10 })
    await useStore.getState().submitRunParameters('target')
    expect(useStore.getState().runs.target).toMatchObject({
      phase: 'confirm', parametersReady: false,
      parameterBindings: [{ name: 'threshold', value: 10 }],
    })

    await useStore.getState().requestRun('target')
    expect(useStore.getState().runs.target).toMatchObject({
      phase: 'parameters', parameterContinuation: { kind: 'run' },
      parameterBindings: [{ name: 'threshold', value: 10 }],
    })
    expect(apiMocks.estimate).toHaveBeenCalledTimes(1)

    useStore.getState().editRunParameters('target')
    await useStore.getState().submitRunParameters('target')
    expect(apiMocks.estimate).toHaveBeenCalledTimes(2)
    expect(apiMocks.run).not.toHaveBeenCalled()
    expect(useStore.getState().runs.target.parametersReady).toBe(false)
  })

  it('invalidates parameter-derived previews immediately and fences a slow preview response', async () => {
    const target = NODE('target', 'filter')
    target.data.config = { threshold: { parameterRef: 'threshold' } }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [],
      parameters: [{ name: 'threshold', type: 'integer' as const, required: true }],
      nodes: [target], edges: [] }
    const first = [{ name: 'threshold', value: 1 }]
    let finish!: (value: ReturnType<typeof previewResult>) => void
    apiMocks.preview.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    useStore.setState({ doc, runs: { target: { phase: 'idle', parameterBindings: first } },
      previews: {}, previewBindings: {}, profileJobs: { target: {
        canvasId: 'c', nodeId: 'target', planIdentity: profilePlanIdentity(doc, 'target'),
        parameterBindings: first, requestGeneration: 1, phase: 'running',
      } } })

    const pending = useStore.getState().runPreview('target')
    await vi.waitFor(() => expect(apiMocks.preview).toHaveBeenCalledTimes(1))
    useStore.getState().setRunParameterBinding('target', { name: 'threshold', value: 2 })
    expect(useStore.getState().previews.target).toBeUndefined()
    expect(useStore.getState().previewBindings.target).toBeUndefined()
    // The old durable job remains tracked; changing a binding is not a cancellation request.
    expect(useStore.getState().profileJobs.target?.phase).toBe('running')

    finish(previewResult('stale'))
    await pending
    expect(useStore.getState().previews.target).toBeUndefined()
  })

  it('fences a slow full-profile preflight after rebinding without cancelling background work', async () => {
    const target = NODE('target', 'filter')
    target.data.config = { threshold: { parameterRef: 'threshold' } }
    const parameters = [{ name: 'threshold', type: 'integer' as const, required: true }]
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], parameters,
      nodes: [target], edges: [] }
    const first = [{ name: 'threshold', value: 1 }]
    let finish!: (value: any) => void
    apiMocks.profileEstimate.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    useStore.setState({ doc, runs: { target: {
      phase: 'idle', parameterBindings: first, parametersReady: true,
      parameterContractFingerprint: JSON.stringify(parameters),
    } }, previews: {}, previewBindings: {}, profileJobs: {} })

    const pending = useStore.getState().prepareFullProfile('target')
    await vi.waitFor(() => expect(apiMocks.profileEstimate).toHaveBeenCalledTimes(1))
    useStore.getState().setRunParameterBinding('target', { name: 'threshold', value: 2 })
    finish({ rows: 10, bytes: 100, placement: 'local', needsConfirm: false,
      targetPortId: 'out', planDigest: 'a'.repeat(64) })
    await pending

    expect(useStore.getState().profileJobs.target).toMatchObject({
      phase: 'estimating', parameterBindings: first,
    })
    expect(apiMocks.cancelRun).not.toHaveBeenCalled()
  })

  it('renames exact parameter refs structurally and blocks dangling deletion or incompatible source types', () => {
    const source = NODE('source')
    source.data.config = { datasetRef: { parameterRef: 'input_data' } }
    const target = NODE('target', 'filter')
    target.data.config = { nested: { exact: { parameterRef: 'input_data' }, literal: 'input_data' } }
    const doc = { id: 'c', version: 1, name: 'test', requirements: [],
      parameters: [{ name: 'input_data', type: 'dataset' as const, required: true }],
      nodes: [source, target], edges: [] }
    useStore.setState({ doc, runs: { source: { phase: 'idle', parameterBindings: [
      { name: 'input_data', value: { kind: 'latest', datasetId: 'dataset-a' } },
    ] } }, previews: {}, previewBindings: {} })

    expect(useStore.getState().setParameters([
      { name: 'robot_data', type: 'dataset', required: true },
    ])).toBeNull()
    expect(useStore.getState().doc.nodes[0].data.config.datasetRef).toEqual({ parameterRef: 'robot_data' })
    expect(useStore.getState().doc.nodes[1].data.config).toEqual({
      nested: { exact: { parameterRef: 'robot_data' }, literal: 'input_data' },
    })
    expect(useStore.getState().runs.source.parameterBindings?.[0].name).toBe('robot_data')

    const deletion = useStore.getState().setParameters([])
    expect(deletion).toMatch(/Cannot remove 'robot_data'/)
    expect(useStore.getState().doc.parameters?.[0].name).toBe('robot_data')

    const typeChange = useStore.getState().setParameters([
      { name: 'robot_data', type: 'string', required: true },
    ])
    expect(typeChange).toMatch(/source\.datasetRef requires dataset/)
    expect(useStore.getState().doc.parameters?.[0].type).toBe('dataset')
  })

  it('keeps debounced autosave and its newer-edit follow-up synchronization quiet', async () => {
    const doc = { ...emptyTestDoc('c'), name: 'Initial name' }
    apiMocks.listCanvases.mockResolvedValue([{ id: doc.id, name: doc.name, version: 1, role: 'owner' }])
    apiMocks.getCanvas.mockResolvedValue(doc)
    await useStore.getState().bootstrap()
    useStore.setState({ toasts: [] })

    let resolveFirst!: (value: { ok: boolean; id: string; version: number }) => void
    apiMocks.saveCanvas.mockReset()
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce({ ok: true, id: doc.id, version: 3 })

    useStore.getState().renameFile('First debounced edit')
    await vi.waitFor(() => expect(apiMocks.saveCanvas).toHaveBeenCalledTimes(1), { timeout: 2_000 })

    useStore.getState().renameFile('Newer edit while saving')
    await vi.waitFor(() => expect(useStore.getState().localDrafts).toMatchObject([{
      doc: { name: 'Newer edit while saving' },
      syncState: 'dirty',
    }]), { timeout: 2_000 })

    resolveFirst({ ok: true, id: doc.id, version: 2 })
    await vi.waitFor(() => {
      expect(apiMocks.saveCanvas).toHaveBeenCalledTimes(2)
      expect(useStore.getState().localDrafts).toEqual([])
      expect(useStore.getState().serverVersion).toBe(3)
      expect(useStore.getState().saved).toBe(true)
    }, { timeout: 2_000 })

    expect(apiMocks.saveCanvas.mock.calls.map(([saved, keepalive, expectedVersion]) => ({
      name: (saved as { name: string }).name,
      keepalive,
      expectedVersion,
    }))).toEqual([
      { name: 'First debounced edit', keepalive: false, expectedVersion: 1 },
      { name: 'Newer edit while saving', keepalive: false, expectedVersion: 2 },
    ])
    expect(useStore.getState().toasts.filter((toast) => toast.kind === 'success')).toEqual([])
  })

  it('keeps a draft whose server base disappeared in Workspace instead of reopening a dead Canvas', async () => {
    const stale = {
      ...emptyTestDoc('deleted-canvas'),
      name: 'Recovered work',
      nodes: [NODE('source')],
    }
    expect(writeCanvasDraft({
      draftId: stale.id,
      principalId: 'alice',
      canvasId: stale.id,
      baseCanvasId: stale.id,
      baseVersion: 2,
      name: stale.name,
      doc: stale,
      createAttemptDoc: null,
      syncState: 'dirty',
      lastLocalEditAt: '2026-08-01T12:00:00.000Z',
    }).ok).toBe(true)
    localStorage.setItem('dp-open-alice', stale.id)
    localStorage.setItem(`dp-canvas-role-alice-${stale.id}`, 'owner')
    window.history.replaceState(null, '', `#/canvas/${stale.id}`)
    apiMocks.getCanvas.mockRejectedValueOnce(new KernelError(404, 'canvas not found'))
    useStore.setState({
      doc: emptyTestDoc('bootstrap-placeholder'),
      view: 'canvas',
      currentDraftId: null,
      firstRunChoice: false,
    })

    await useStore.getState().bootstrap()

    expect(apiMocks.getCanvas).toHaveBeenCalledOnce()
    expect(apiMocks.getCanvas).toHaveBeenCalledWith(stale.id)
    expect(useStore.getState()).toMatchObject({
      view: 'workspace',
      currentDraftId: null,
      firstRunChoice: false,
      doc: { id: 'bootstrap-placeholder' },
      localDrafts: [{
        canvasId: stale.id,
        syncState: 'conflict',
        lastError: expect.stringContaining('local draft is preserved'),
      }],
    })
    expect(localStorage.getItem(`dp-canvas-role-alice-${stale.id}`)).toBeNull()
    expect(apiMocks.activeRuns).not.toHaveBeenCalled()
    expect(apiMocks.profileJobs).not.toHaveBeenCalled()

    // The preserved graph remains explicitly inspectable without probing server execution endpoints.
    expect(useStore.getState().openLocalDraft(stale.id)).toBe(true)
    expect(useStore.getState()).toMatchObject({
      view: 'canvas',
      currentDraftId: stale.id,
      doc: { id: stale.id, name: stale.name },
      canvasRole: null,
    })
    expect(apiMocks.activeRuns).not.toHaveBeenCalled()
    expect(apiMocks.profileJobs).not.toHaveBeenCalled()
    window.history.replaceState(null, '', '#/workspace')
  })

  it('re-enables only an availability conflict when the server Canvas is shared again', async () => {
    const recovered = { ...emptyTestDoc('shared-again'), name: 'Shared again', nodes: [NODE('source')] }
    const versionConflict = { ...emptyTestDoc('version-conflict'), name: 'Concurrent edit' }
    for (const [doc, lastError, lastLocalEditAt] of [
      [recovered, UNAVAILABLE_DRAFT_BASE_MESSAGE, '2026-08-01T14:00:00.000Z'],
      [versionConflict, 'The server Canvas changed. Your local draft is preserved.', '2026-08-01T13:00:00.000Z'],
    ] as const) {
      expect(writeCanvasDraft({
        draftId: doc.id,
        principalId: 'alice',
        canvasId: doc.id,
        baseCanvasId: doc.id,
        baseVersion: 2,
        name: doc.name,
        doc,
        createAttemptDoc: null,
        syncState: 'conflict',
        lastLocalEditAt,
        lastError,
      }).ok).toBe(true)
    }
    apiMocks.listCanvases.mockResolvedValue([
      { id: recovered.id, name: recovered.name, version: 2, role: 'editor' },
      { id: versionConflict.id, name: versionConflict.name, version: 3, role: 'editor' },
    ])
    localStorage.setItem('dp-open-alice', recovered.id)
    window.history.replaceState(null, '', '#/workspace')

    await useStore.getState().bootstrap()

    expect(useStore.getState().localDrafts.find((draft) => draft.draftId === recovered.id)).toMatchObject({
      syncState: 'dirty',
      lastError: undefined,
    })
    expect(useStore.getState().localDrafts.find((draft) => draft.draftId === versionConflict.id)).toMatchObject({
      syncState: 'conflict',
      lastError: 'The server Canvas changed. Your local draft is preserved.',
    })
    expect(useStore.getState()).toMatchObject({
      view: 'workspace',
      currentDraftId: recovered.id,
      doc: { id: recovered.id },
      canvasRole: 'editor',
    })
    expect(apiMocks.activeRuns).toHaveBeenCalledWith(recovered.id)
    expect(apiMocks.profileJobs).toHaveBeenCalledWith(recovered.id)

    // The reverse transition is durable across a browser-style draft reload.
    useStore.getState().refreshLocalDrafts()
    const reloaded = useStore.getState().localDrafts.find((draft) => draft.draftId === recovered.id)
    expect(reloaded).toMatchObject({ syncState: 'dirty' })
    expect(reloaded).not.toHaveProperty('lastError')
    window.history.replaceState(null, '', '#/workspace')
  })

  it('keeps viewer re-shares and downgrades as readable recovery choices until edit access returns', async () => {
    const reshared = { ...emptyTestDoc('viewer-reshare'), name: 'Viewer re-share', nodes: [NODE('source')] }
    const downgraded = { ...emptyTestDoc('viewer-downgrade'), name: 'Viewer downgrade' }
    const versionConflict = { ...emptyTestDoc('viewer-version-conflict'), name: 'Concurrent edit' }
    for (const [doc, syncState, lastError, lastLocalEditAt] of [
      [reshared, 'conflict', UNAVAILABLE_DRAFT_BASE_MESSAGE, '2026-08-01T17:00:00.000Z'],
      [downgraded, 'dirty', undefined, '2026-08-01T16:00:00.000Z'],
      [versionConflict, 'conflict', 'The server Canvas changed. Your local draft is preserved.', '2026-08-01T15:00:00.000Z'],
    ] as const) {
      expect(writeCanvasDraft({
        draftId: doc.id,
        principalId: 'alice',
        canvasId: doc.id,
        baseCanvasId: doc.id,
        baseVersion: 2,
        name: doc.name,
        doc,
        createAttemptDoc: null,
        syncState,
        lastLocalEditAt,
        lastError,
      }).ok).toBe(true)
    }
    const viewerFiles = [reshared, downgraded, versionConflict].map((doc) => ({
      id: doc.id, name: doc.name, version: 2, role: 'viewer' as const,
    }))
    apiMocks.listCanvases.mockResolvedValue(viewerFiles)
    localStorage.setItem('dp-open-alice', reshared.id)
    window.history.replaceState(null, '', '#/workspace')

    await useStore.getState().bootstrap()

    for (const id of [reshared.id, downgraded.id]) {
      expect(useStore.getState().localDrafts.find((draft) => draft.draftId === id)).toMatchObject({
        syncState: 'conflict',
        lastError: READ_ONLY_DRAFT_BASE_MESSAGE,
      })
    }
    expect(useStore.getState().localDrafts.find((draft) => draft.draftId === versionConflict.id)).toMatchObject({
      syncState: 'conflict',
      lastError: 'The server Canvas changed. Your local draft is preserved.',
    })
    expect(useStore.getState()).toMatchObject({
      view: 'workspace',
      currentDraftId: reshared.id,
      canvasRole: 'viewer',
    })
    expect(apiMocks.activeRuns).not.toHaveBeenCalled()
    expect(apiMocks.profileJobs).not.toHaveBeenCalled()

    apiMocks.listCanvases.mockResolvedValue(viewerFiles.map((file) => ({ ...file, role: 'editor' as const })))
    await useStore.getState().bootstrap()

    for (const id of [reshared.id, downgraded.id]) {
      expect(useStore.getState().localDrafts.find((draft) => draft.draftId === id)).toMatchObject({
        syncState: 'dirty',
        lastError: undefined,
      })
    }
    expect(useStore.getState().localDrafts.find((draft) => draft.draftId === versionConflict.id)).toMatchObject({
      syncState: 'conflict',
      lastError: 'The server Canvas changed. Your local draft is preserved.',
    })
    expect(useStore.getState().canvasRole).toBe('editor')
    expect(apiMocks.activeRuns).toHaveBeenCalledWith(reshared.id)
    expect(apiMocks.profileJobs).toHaveBeenCalledWith(reshared.id)
    window.history.replaceState(null, '', '#/workspace')
  })

  it('still restores a never-synced local-only draft without server recovery probes', async () => {
    const localOnly = {
      ...emptyTestDoc('local-only'),
      name: 'Offline work',
      nodes: [NODE('source')],
    }
    expect(writeCanvasDraft({
      draftId: localOnly.id,
      principalId: 'alice',
      canvasId: localOnly.id,
      baseCanvasId: null,
      baseVersion: null,
      name: localOnly.name,
      doc: localOnly,
      createAttemptDoc: localOnly,
      syncState: 'dirty',
      lastLocalEditAt: '2026-08-01T13:00:00.000Z',
    }).ok).toBe(true)
    localStorage.setItem('dp-open-alice', localOnly.id)
    window.history.replaceState(null, '', `#/canvas/${localOnly.id}`)

    await useStore.getState().bootstrap()

    expect(useStore.getState()).toMatchObject({
      view: 'canvas',
      currentDraftId: localOnly.id,
      doc: { id: localOnly.id, name: localOnly.name },
      canvasRole: 'owner',
    })
    expect(apiMocks.getCanvas).not.toHaveBeenCalled()
    expect(apiMocks.activeRuns).not.toHaveBeenCalled()
    expect(apiMocks.profileJobs).not.toHaveBeenCalled()
    window.history.replaceState(null, '', '#/workspace')
  })
})

function emptyTestDoc(id: string) {
  return { id, version: 1, name: id, nodes: [], edges: [] }
}

function previewResult(value: string) {
  return {
    columns: [{ name: 'value', type: 'VARCHAR', capabilities: [] }],
    rows: [{ value }], rowCount: 1, hasMore: false, truncated: false,
  }
}
