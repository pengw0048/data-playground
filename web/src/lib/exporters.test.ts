import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({ preview: vi.fn() }))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_target, property) => property === 'preview' ? apiMocks.preview : vi.fn(async () => ({})),
  }),
  KernelError: class KernelError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
  setApiUser: vi.fn(),
}))

import { exportNode } from './exporters'
import { previewPlanIdentity, useStore, type PreviewState } from '../store/graph'
import type { CanvasDoc, CanvasNode } from '../types/graph'
import type { SampleResult } from '../types/api'

const NODE = (id: string, type = 'source'): CanvasNode => ({
  id, type, position: { x: 0, y: 0 },
  data: { title: id, config: {}, status: 'draft', history: [] },
})

function result(value: string): SampleResult {
  return {
    columns: [{ name: 'value', type: 'string', capabilities: [] }],
    rows: [{ value }], rowCount: 1, truncated: true, hasMore: false,
  }
}

function boundPreview(doc: CanvasDoc, nodeId: string, value: string): PreviewState {
  return {
    canvasId: doc.id, nodeId, planIdentity: previewPlanIdentity(doc, nodeId), requestGeneration: 1,
    offset: 0, result: result(value),
  }
}

function pipeline(): CanvasDoc {
  const source = NODE('source')
  source.data.config = { uri: 'events.parquet' }
  const target = NODE('target', 'filter')
  target.data.config = { predicate: 'event = purchase' }
  return {
    id: 'canvas', version: 1, name: 'test', requirements: [], nodes: [source, target],
    edges: [{ id: 'source-target', source: 'source', target: 'target', data: { wire: 'dataset' } }],
  }
}

describe('node sample export freshness', () => {
  let downloads: string[]
  let blobs: Blob[]

  beforeEach(() => {
    apiMocks.preview.mockReset()
    downloads = []
    blobs = []
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true, value: vi.fn((blob: Blob) => { blobs.push(blob); return 'blob:export' }),
    })
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true, value: vi.fn(),
    })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push(this.download)
    })
    useStore.setState({
      doc: pipeline(), previews: {}, toasts: [], selectedId: null, selectedIds: [], canvasRole: 'owner',
    })
  })

  it.each([
    ['configuration edit', (doc: CanvasDoc) => { doc.nodes[1].data.config.predicate = 'event = view' }],
    ['topology edit', (doc: CanvasDoc) => { doc.edges = [] }],
    ['metric rename', (doc: CanvasDoc) => {
      doc.nodes[1].type = 'metric'
      doc.nodes[1].data.title = 'Average revenue'
    }],
  ])('refreshes instead of exporting a stale cached result after a %s', async (_label, mutate) => {
    const oldDoc = pipeline()
    if (_label === 'metric rename') {
      oldDoc.nodes[1].type = 'metric'
      oldDoc.nodes[1].data.title = 'Revenue'
    }
    const currentDoc = structuredClone(oldDoc)
    mutate(currentDoc)
    useStore.setState({ doc: currentDoc, previews: { target: boundPreview(oldDoc, 'target', 'stale') } })
    apiMocks.preview.mockResolvedValueOnce(result('fresh'))

    await exportNode('target')

    expect(apiMocks.preview).toHaveBeenCalledWith(currentDoc, 'target', 500)
    expect(downloads).toHaveLength(2)
    expect(await blobs[0].text()).toContain('fresh')
    expect(await blobs[0].text()).not.toContain('stale')
  })

  it('reuses a current cached result across presentation-only movement, status, edge-id, and selection edits', async () => {
    const previewDoc = pipeline()
    const currentDoc = structuredClone(previewDoc)
    currentDoc.nodes[0].position = { x: 800, y: 400 }
    currentDoc.nodes[1].data.status = 'running'
    currentDoc.edges[0].id = 'visual-edge-id'
    useStore.setState({
      doc: currentDoc,
      previews: { target: boundPreview(previewDoc, 'target', 'current') },
      selectedId: 'source', selectedIds: ['source'],
    })

    await exportNode('target')

    expect(apiMocks.preview).not.toHaveBeenCalled()
    expect(downloads).toEqual(['target.json', 'target.csv'])
  })

  it('drops a refreshed export when the graph changes before its response arrives', async () => {
    let finish!: (value: SampleResult) => void
    apiMocks.preview.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const request = exportNode('target')
    const edited = structuredClone(useStore.getState().doc)
    edited.nodes[1].data.config.predicate = 'event = view'
    useStore.setState({ doc: edited })

    finish(result('old graph'))
    await request

    expect(downloads).toEqual([])
    expect(useStore.getState().toasts.at(-1)?.msg).toMatch(/graph changed/i)
  })

  it('makes concurrent export refreshes latest-wins when responses are reversed', async () => {
    let finishFirst!: (value: SampleResult) => void
    let finishSecond!: (value: SampleResult) => void
    let finishThird!: (value: SampleResult) => void
    apiMocks.preview
      .mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishSecond = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishThird = resolve }))

    const first = exportNode('target')
    const second = exportNode('target')
    finishSecond(result('newer'))
    await second
    expect(downloads).toHaveLength(2)

    // A completed latest request may clear its active map entry while the superseded first request is
    // still in flight. A third generation must remain globally unique so that first response cannot
    // become current again merely because its old number was reused.
    const third = exportNode('target')
    finishFirst(result('older'))
    await first
    expect(downloads).toHaveLength(2)
    finishThird(result('newest'))
    await third
    expect(downloads).toHaveLength(4)
    expect(useStore.getState().toasts.filter((toast) => /Exported target/.test(toast.msg))).toHaveLength(2)
  })
})
