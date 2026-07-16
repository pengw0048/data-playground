import { describe, it, expect, beforeEach, vi } from 'vitest'

// the store module runs autosave side-effects at import; stub the network client so nothing escapes.
// (Autosave is gated on _bootstrapped=false at import, so no PUT fires here anyway.)
const apiMocks = vi.hoisted(() => ({
  listCanvases: vi.fn(), getCanvas: vi.fn(), createCanvas: vi.fn(), deleteCanvas: vi.fn(), preview: vi.fn(),
  estimate: vi.fn(), run: vi.fn(), profileEstimate: vi.fn(), profileIdentity: vi.fn(), fullProfile: vi.fn(), runStatus: vi.fn(), cancelRun: vi.fn(),
  activeRuns: vi.fn(), profileJobs: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_target, property) => property === 'listCanvases'
      ? apiMocks.listCanvases
      : property === 'getCanvas'
        ? apiMocks.getCanvas
        : property === 'createCanvas'
          ? apiMocks.createCanvas
          : property === 'deleteCanvas'
            ? apiMocks.deleteCanvas
          : property === 'preview'
            ? apiMocks.preview
            : property === 'estimate'
              ? apiMocks.estimate
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
          : async () => ({}),
  }),
  KernelError: class KernelError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } },
  setApiUser: vi.fn(),
}))

import {
  currentPreviews, previewPlanIdentity, profilePlanIdentity, useStore,
} from './graph'
import { KernelError } from '../api/client'

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

describe('graph store — core authority ops', () => {
  beforeEach(() => {
    // start each test from a known empty doc
    localStorage.clear()
    apiMocks.listCanvases.mockReset().mockResolvedValue([])
    apiMocks.getCanvas.mockReset()
    apiMocks.createCanvas.mockReset().mockImplementation(async (doc: { id: string }) => (
      { ok: true, id: doc.id, created: true }
    ))
    apiMocks.deleteCanvas.mockReset().mockResolvedValue({ ok: true })
    apiMocks.preview.mockReset()
    apiMocks.estimate.mockReset().mockResolvedValue({ rows: 10, bytes: 100, placement: 'local', needsConfirm: false })
    apiMocks.run.mockReset().mockResolvedValue({
      runId: 'run-store-test', status: 'running', jobType: 'run', targetNodeId: 'target',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [], outputs: [],
    })
    apiMocks.profileEstimate.mockReset().mockResolvedValue({
      rows: 10, bytes: 100, placement: 'local', needsConfirm: false, planDigest: 'a'.repeat(64),
    })
    apiMocks.profileIdentity.mockReset().mockResolvedValue({ planDigest: 'a'.repeat(64) })
    apiMocks.fullProfile.mockReset()
    apiMocks.runStatus.mockReset()
    apiMocks.cancelRun.mockReset().mockImplementation(async (runId: string) => ({
      runId, status: 'cancelled', jobType: 'run',
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    }))
    apiMocks.activeRuns.mockReset().mockResolvedValue([])
    apiMocks.profileJobs.mockReset().mockResolvedValue([])
    useStore.setState({ currentUser: { id: 'alice', name: 'Alice' } })
    useStore.setState({
      doc: { id: 'c', version: 1, name: 'test', nodes: [], edges: [], requirements: [] },
      canvasRole: 'owner', past: [], future: [], toasts: [], agentOpen: false, accessDenied: false, kernelUp: false,
      profileJobs: {},
    })
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
    expect(currentPreviews(doc, useStore.getState().previews)).toEqual({})
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
      expect.objectContaining({ id: doc.id }), 'section', false,
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
      expect.objectContaining({ id: doc.id }), 'section', false,
    )

    apiMocks.estimate.mockClear()
    useStore.getState().rerunAll()
    await vi.waitFor(() => expect(apiMocks.estimate).toHaveBeenCalledWith(
      expect.objectContaining({ id: doc.id }), 'section',
    ))
    expect(useStore.getState().toasts).toHaveLength(0)
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
    resolveJob({ runId: 'profile-old', status: 'queued', jobType: 'profile', targetNodeId: 'source',
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
    useStore.setState({ doc })
    apiMocks.profileEstimate.mockResolvedValueOnce({
      rows: null, bytes: null, placement: 'local', needsConfirm: true, planDigest: 'a'.repeat(64),
    })

    await useStore.getState().prepareFullProfile('source')

    expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'preflight', estimate: { needsConfirm: true },
    })
    expect(apiMocks.fullProfile).not.toHaveBeenCalled()

    apiMocks.fullProfile.mockResolvedValueOnce({
      runId: 'profile-confirmed', status: 'done', jobType: 'profile', targetNodeId: 'source',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
    })
    await useStore.getState().startFullProfile('source')

    expect(apiMocks.fullProfile).toHaveBeenCalledWith(
      doc, 'source', useStore.getState().profileJobs.source.planDigest, expect.any(String), true,
    )
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

  it('reuses one submission id across bounded ambiguous submission retries', async () => {
    const doc = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    useStore.setState({ doc })
    await useStore.getState().prepareFullProfile('source')
    apiMocks.fullProfile
      .mockRejectedValueOnce(new Error('network response lost'))
      .mockRejectedValueOnce(new KernelError(503, 'hub restarting'))
      .mockResolvedValueOnce({
        runId: 'profile-adopted', status: 'done', jobType: 'profile', targetNodeId: 'source',
        planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
        rowsProcessed: 10, ms: 10, placement: 'local', perNode: [],
        profile: { columns: [], rowCount: 10, sampled: false },
      })

    await useStore.getState().startFullProfile('source')

    expect(apiMocks.fullProfile).toHaveBeenCalledTimes(3)
    const submissionIds = apiMocks.fullProfile.mock.calls.map((call) => call[3])
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
      runId: 'profile-reconciled-later', status: 'done', jobType: 'profile', targetNodeId: 'source',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    })

    await useStore.getState().startFullProfile('source')

    expect(apiMocks.fullProfile).toHaveBeenCalledWith(doc, 'source', 'a'.repeat(64), submissionId, true)
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
      runId: 'profile-cancel-after-adopt', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-response-lost', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'superseded-submission-run', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
    const oldSubmissionId = apiMocks.fullProfile.mock.calls[0][3]
    expect(apiMocks.fullProfile.mock.calls.slice(0, 4).every((call) => call[3] === oldSubmissionId)).toBe(true)

    // The user can move on to the new graph. Orphan reconciliation owns the old captured doc/key and
    // must not write through this newer preflight when its response finally arrives.
    await useStore.getState().prepareFullProfile('source')
    const newGeneration = useStore.getState().profileJobs.source.requestGeneration
    expect(useStore.getState().profileJobs.source.phase).toBe('preflight')
    finishOrphan({
      runId: 'old-orphaned-scan', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'removed-node-profile', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'alice-detached-profile', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'detached-profile-role-revoked', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-race', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-active-200', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-transient-error', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-status-transient', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-terminal-race', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-unverified', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-compact-terminal', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-role-revoked', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-cancel-unknown', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'finished-away', status: 'done', jobType: 'profile', targetNodeId: 'source',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 4, totalRows: 4, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 4, sampled: false },
    }])
    apiMocks.activeRuns.mockRejectedValueOnce(new Error('transient active-run lookup failure'))

    useStore.getState().loadDoc(current, 'owner')
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.status?.runId).toBe('finished-away'))
    expect(useStore.getState().profileJobs.source?.phase).toBe('done')

    apiMocks.profileJobs.mockResolvedValueOnce([{
      runId: 'stale-plan', status: 'done', jobType: 'profile', targetNodeId: 'source',
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
        runId: 'current-plan-order-1', status: 'done', jobType: 'profile', targetNodeId: 'source',
        planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
        rowsProcessed: 4, totalRows: 4, ms: 10, placement: 'local', perNode: [],
        profile: { columns: [], rowCount: 4, sampled: false },
      }
      const staleAttempt = {
        runId: `stale-plan-order-2-${staleStatus}-${staleFirst ? 'first' : 'last'}`,
        status: staleStatus, jobType: 'profile', targetNodeId: 'source',
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
      runId: 'viewer-stale-active', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'viewer-current-active', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'viewer-verifying-active', status: 'running', jobType: 'profile', targetNodeId: 'source',
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

    finishIdentity({ planDigest: active.planDigest })
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
      runId: `terminal-verifying-${role}`, status: 'done', jobType: 'profile', targetNodeId: 'source',
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

    finishIdentity({ planDigest: terminal.planDigest })
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
      runId: 'alice-complete-profile', status: 'done', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'identity-retry-active', status: 'running', jobType: 'profile', targetNodeId: 'source',
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

    finishIdentity({ planDigest: 'a'.repeat(64) })
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source).toMatchObject({
      phase: 'running', identityVerified: true,
      status: { runId: recovered.runId, profile: { rowCount: 2 } },
    }))
  })

  it('fails closed after persistent identity failure while retaining exact cancellation identity', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const recovered = {
      runId: 'identity-failed-active', status: 'queued', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'active-fallback', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'active-fallback', status: 'cancelled', jobType: 'profile', targetNodeId: 'source',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    })
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.phase).toBe('cancelled'))
  })

  it('does not let a delayed queued poll response regress a running profile', async () => {
    const current = { id: 'c', version: 1, name: 'test', requirements: [], nodes: [NODE('source')], edges: [] }
    const planDigest = 'a'.repeat(64)
    const base = {
      runId: 'poll-monotonic', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'poll-identity', status: 'running', jobType: 'profile', targetNodeId: 'source',
      planDigest, profileAttemptOrder: 3, rowsProcessed: 1, totalRows: 10,
      ms: 10, placement: 'local', perNode: [],
    }])
    apiMocks.runStatus.mockResolvedValueOnce({
      runId: 'poll-identity', status: 'done', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'profile-pruned-detail', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'projection-first', status: 'done', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'active-after-empty', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'old-projection', status: 'done', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'new-active-retry', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'same-attempt', status: 'running', jobType: 'profile', targetNodeId: 'source',
      planDigest, profileAttemptOrder: 6, rowsProcessed: 1, totalRows: 10,
      ms: 10, placement: 'local', perNode: [],
    }])
    apiMocks.runStatus.mockImplementationOnce(() => new Promise(() => {}))

    useStore.getState().loadDoc(current, 'owner')
    await vi.waitFor(() => expect(useStore.getState().profileJobs.source?.phase).toBe('running'))
    finishProjection([{
      runId: 'same-attempt', status: 'queued', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'provisional-active', status: 'running', jobType: 'profile', targetNodeId: 'source',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 1, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    }])
    let finishStatus!: (status: any) => void
    apiMocks.runStatus.mockImplementationOnce(() => new Promise((resolve) => { finishStatus = resolve }))

    useStore.getState().loadDoc(current, 'owner')

    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('provisional-active'))
    finishProjection([{
      runId: 'authoritative-terminal', status: 'done', jobType: 'profile', targetNodeId: 'source',
      planDigest, profileAttemptOrder: 2, rowsProcessed: 10, totalRows: 10, ms: 20, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    }])
    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('authoritative-terminal'))
    finishStatus({
      runId: 'provisional-active', status: 'running', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'newer-reattach', status: 'done', jobType: 'profile', targetNodeId: 'source',
      planDigest, profileAttemptOrder: 2, rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    }])
    await vi.waitFor(() => expect(
      useStore.getState().profileJobs.source?.status?.runId,
    ).toBe('newer-reattach'))
    finishOld([{
      runId: 'older-reattach', status: 'done', jobType: 'profile', targetNodeId: 'source',
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
      runId: 'old-recovered-profile', status: 'done', jobType: 'profile', targetNodeId: 'source',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 5, totalRows: 5,
      ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 5, sampled: false },
    }])
    await Promise.resolve()
    await Promise.resolve()
    expect(useStore.getState().profileJobs.source?.phase).toBe('queued')
    expect(useStore.getState().profileJobs.source?.status).toBeUndefined()

    finishSubmission({
      runId: 'new-user-profile', status: 'done', jobType: 'profile', targetNodeId: 'source',
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
    expect(apiMocks.createCanvas.mock.calls[0]).toHaveLength(1)
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
