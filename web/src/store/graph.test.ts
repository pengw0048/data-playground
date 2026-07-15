import { describe, it, expect, beforeEach, vi } from 'vitest'

// the store module runs autosave side-effects at import; stub the network client so nothing escapes.
// (Autosave is gated on _bootstrapped=false at import, so no PUT fires here anyway.)
const apiMocks = vi.hoisted(() => ({ listCanvases: vi.fn(), getCanvas: vi.fn(), createCanvas: vi.fn(), preview: vi.fn() }))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_target, property) => property === 'listCanvases'
      ? apiMocks.listCanvases
      : property === 'getCanvas'
        ? apiMocks.getCanvas
        : property === 'createCanvas'
          ? apiMocks.createCanvas
          : property === 'preview'
            ? apiMocks.preview
          : async () => ({}),
  }),
  KernelError: class KernelError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } },
  setApiUser: vi.fn(),
}))

import { useStore } from './graph'
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
    apiMocks.createCanvas.mockReset().mockResolvedValue({ ok: true })
    apiMocks.preview.mockReset()
    useStore.setState({ currentUser: { id: 'alice', name: 'Alice' } })
    useStore.setState({
      doc: { id: 'c', version: 1, name: 'test', nodes: [], edges: [], requirements: [] },
      canvasRole: 'owner', past: [], future: [], toasts: [], agentOpen: false, accessDenied: false, kernelUp: false,
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
    let finishCreate!: (value: { ok: boolean }) => void
    apiMocks.createCanvas.mockImplementationOnce(() => new Promise((resolve) => { finishCreate = resolve }))
    const before = useStore.getState().doc

    const creating = useStore.getState().newFile()
    useStore.getState().setView('files')
    finishCreate({ ok: true })

    expect(await creating).toEqual({ ok: false })
    expect(useStore.getState().doc).toBe(before)
    expect(useStore.getState().view).toBe('files')
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
