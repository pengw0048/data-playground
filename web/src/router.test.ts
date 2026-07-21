import { afterEach, describe, expect, it, vi } from 'vitest'
import { initRouter, parseHash, routeHash } from './router'

describe('Workspace routes', () => {
  afterEach(() => { window.location.hash = '' })

  it('round-trips an opaque stable Workspace resource ID', () => {
    const resourceId = 'dataset:registration/with spaces'
    window.location.hash = routeHash('workspace', undefined, resourceId)
    expect(parseHash()).toEqual({ view: 'workspace', workspaceResourceId: resourceId })
  })

  it('round-trips a lexical query with the selected stable result', () => {
    const resourceId = 'dataset:registration/with spaces'
    window.location.hash = routeHash('workspace', undefined, resourceId, 'robot observations')
    expect(parseHash()).toEqual({
      view: 'workspace', workspaceResourceId: resourceId, workspaceQuery: 'robot observations',
    })
  })

  it('round-trips Datasets scope state without reusing the mixed-search query', () => {
    const resourceId = 'dataset:registration/with spaces'
    const datasetQuery = new URLSearchParams({
      dq: 'robot hands', folder: 'robotics/curated', tags: 'gold,ego', columns: 'frame_id',
      sort: 'updated', order: 'desc', match: 'meaning',
    }).toString()
    window.location.hash = routeHash(
      'workspace', undefined, resourceId, 'must-not-leak', undefined, undefined, undefined,
      'datasets', datasetQuery,
    )
    expect(parseHash()).toEqual({
      view: 'workspace', workspaceResourceId: resourceId, workspaceScope: 'datasets',
      workspaceDatasetQuery: datasetQuery,
    })
    expect(window.location.hash).not.toContain('q=must-not-leak')
  })

  it('deliberately redirects former Recents and Tables URLs to Workspace', () => {
    window.location.hash = '#/files'
    expect(parseHash()).toEqual({ view: 'workspace' })
    window.location.hash = '#/tables'
    expect(parseHash()).toEqual({ view: 'workspace' })
  })

  it('round-trips Jobs filters and run/artifact deep-link identity', () => {
    const query = new URLSearchParams({ status: 'failed', canvas: 'canvas-1', run: 'run-1', output: 'write:out' }).toString()
    window.location.hash = routeHash('jobs', undefined, undefined, undefined, query)
    expect(parseHash()).toEqual({ view: 'jobs', jobsQuery: query })
  })

  it('opens an exact retained distribution-report link in Jobs detail', () => {
    const reportId = 'a'.repeat(32)
    window.location.hash = `#/distribution-reports/${reportId}`
    expect(parseHash()).toEqual({ view: 'jobs', jobsQuery: `report=${reportId}` })
  })

  it('preserves a comparison identity on retained-report deep links', () => {
    const report = 'a'.repeat(32), compare = 'b'.repeat(32)
    window.location.hash = `#/distribution-reports/${report}?compare=${compare}`
    expect(parseHash()).toEqual({ view: 'jobs', jobsQuery: `report=${report}&compare=${compare}` })
  })

  it('round-trips Inbox filter query', () => {
    const query = new URLSearchParams({ filter: 'unread' }).toString()
    window.location.hash = routeHash('inbox', undefined, undefined, undefined, undefined, undefined, query)
    expect(parseHash()).toEqual({ view: 'inbox', inboxQuery: query })
  })

  it('round-trips a canvas node deep link', () => {
    window.location.hash = routeHash('canvas', 'canvas-1', undefined, undefined, undefined, 'write-1')
    expect(parseHash()).toEqual({ view: 'canvas', canvasId: 'canvas-1', nodeId: 'write-1' })
  })

  it('round-trips an exact Transform upgrade context without mixing it into filters', () => {
    window.location.hash = routeHash(
      'transforms', undefined, undefined, undefined, undefined, undefined, undefined,
      undefined, undefined, 'tr_exact', 'v2', 'q=robot&source=promoted', 'canvas-1', 'node-1',
    )
    expect(parseHash()).toEqual({
      view: 'transforms', transformId: 'tr_exact', transformVersion: 'v2',
      transformCanvasId: 'canvas-1', transformNodeId: 'node-1',
      transformQuery: 'q=robot&source=promoted',
    })
  })

  it('does not let a stale invalid Canvas route clear a newer node reveal', async () => {
    let resolveOldOpen!: (opened: boolean) => void
    const oldOpen = new Promise<boolean>((resolve) => { resolveOldOpen = resolve })
    const state = {
      view: 'canvas' as const,
      doc: { id: 'canvas-new', nodes: [{ id: 'node-new' }] },
      selectedId: null as string | null,
      workspaceResourceId: null,
      workspaceSearchQuery: '',
      workspaceScope: 'all' as const,
      workspaceDatasetQuery: '',
      jobsQuery: '', inboxQuery: '', transformResourceId: null, transformVersion: null,
      transformUpgradeCanvasId: null, transformUpgradeNodeId: null, transformLibraryQuery: '',
      nodeRevealRequest: null as { canvasId: string; nodeId: string } | null,
    }
    const openFile = vi.fn(async (id: string) => id === 'canvas-old' ? oldOpen : false)
    const store = {
      getState: () => ({
        ...state,
        setView: (view: typeof state.view) => { state.view = view },
        select: (id: string | null) => { state.selectedId = id },
        requestNodeReveal: (canvasId: string, nodeId: string) => {
          state.nodeRevealRequest = { canvasId, nodeId }
        },
        clearNodeReveal: () => { state.nodeRevealRequest = null },
        pushToast: vi.fn(), setWorkspaceResource: vi.fn(), setWorkspaceSearchQuery: vi.fn(),
        setWorkspaceScope: vi.fn(), setWorkspaceDatasetQuery: vi.fn(), setJobsQuery: vi.fn(),
        setInboxQuery: vi.fn(), setTransformResource: vi.fn(), setTransformLibraryQuery: vi.fn(),
        openFile,
      }),
      subscribe: vi.fn(),
    }
    initRouter(store)

    history.replaceState(null, '', '#/canvas/canvas-old?node=missing')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await vi.waitFor(() => expect(openFile).toHaveBeenCalledWith('canvas-old'))

    history.replaceState(null, '', '#/canvas/canvas-new?node=node-new')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await vi.waitFor(() => expect(state.nodeRevealRequest).toEqual({
      canvasId: 'canvas-new', nodeId: 'node-new',
    }))

    resolveOldOpen(false)
    await vi.waitFor(() => {
      expect(state.selectedId).toBe('node-new')
      expect(state.nodeRevealRequest).toEqual({ canvasId: 'canvas-new', nodeId: 'node-new' })
      expect(location.hash).toBe('#/canvas/canvas-new?node=node-new')
    })
  })
})
