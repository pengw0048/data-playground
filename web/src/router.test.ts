import { afterEach, describe, expect, it, vi } from 'vitest'
import { initRouter, parseHash, resetRouterForTests, routeHash } from './router'
import type { DpView } from './store/graph'
import { ownsNavigation, startNavigation } from './navigationOwnership'

describe('Workspace routes', () => {
  afterEach(() => { resetRouterForTests(); window.location.hash = '' })

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
      view: 'canvas' as DpView,
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
        applyRoute: (route: { view: DpView }) => { state.view = route.view },
        setView: (view: DpView) => { state.view = view },
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
      subscribe: vi.fn(() => () => {}),
    }
    const bootstrapToken = startNavigation()
    const router = initRouter(store, bootstrapToken)

    // A user Canvas open owns a newer token while its fetch is pending. Bootstrap settling must
    // release only its own suppression, never re-apply the still-old URL and cancel that request.
    startNavigation()
    router.settleBootstrap(bootstrapToken)
    expect(openFile).not.toHaveBeenCalled()

    history.replaceState(null, '', '#/canvas/canvas-old?node=missing')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await vi.waitFor(() => expect(openFile).toHaveBeenCalledWith('canvas-old', {
      navigationToken: expect.any(Number),
    }))

    history.replaceState(null, '', '#/inbox')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await vi.waitFor(() => expect(state.view).toBe('inbox'))
    expect(location.hash).toBe('#/inbox')

    history.replaceState(null, '', '#/jobs?status=failed')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await vi.waitFor(() => expect(state.view).toBe('jobs'))
    expect(location.hash).toBe('#/jobs?status=failed')

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

  it('keeps a user Canvas open owned through stale bootstrap settle and browser history', async () => {
    let releaseInitialA!: () => void
    let releaseUserB!: () => void
    const initialA = new Promise<void>((resolve) => { releaseInitialA = resolve })
    const userB = new Promise<void>((resolve) => { releaseUserB = resolve })
    let firstA = true
    let firstB = true
    const state = {
      view: 'canvas' as DpView,
      doc: { id: 'canvas-a', nodes: [] as { id: string }[] }, selectedId: null as string | null,
      workspaceResourceId: null, workspaceSearchQuery: '', workspaceScope: 'all' as const,
      workspaceDatasetQuery: '', jobsQuery: '', inboxQuery: '', transformResourceId: null,
      transformVersion: null, transformUpgradeCanvasId: null, transformUpgradeNodeId: null,
      transformLibraryQuery: '',
    }
    const subscribers = new Set<(snapshot: typeof state) => void>()
    const publish = () => { for (const subscriber of subscribers) subscriber({ ...state }) }
    const openFile = vi.fn(async (id: string, options?: { navigationToken?: number }) => {
      const navigationToken = options?.navigationToken ?? startNavigation()
      if (id === 'canvas-a' && firstA) { firstA = false; await initialA }
      if (id === 'canvas-b' && firstB) { firstB = false; await userB }
      // This models the real store's post-await ownership check before it installs a Canvas.
      if (!ownsNavigation(navigationToken)) return false
      state.doc = { id, nodes: [] }
      state.view = 'canvas'
      publish()
      return true
    })
    const store = {
      getState: () => ({
        ...state,
        applyRoute: (route: { view: DpView }) => { state.view = route.view; publish() },
        select: (id: string | null) => { state.selectedId = id; publish() },
        requestNodeReveal: vi.fn(), clearNodeReveal: vi.fn(), pushToast: vi.fn(), openFile,
      }),
      subscribe: (subscriber: (snapshot: typeof state) => void) => {
        subscribers.add(subscriber)
        return () => { subscribers.delete(subscriber) }
      },
    }
    history.replaceState(null, '', '#/canvas/canvas-a')
    const bootstrapToken = startNavigation()
    const router = initRouter(store, bootstrapToken)
    const bootstrapOpen = openFile('canvas-a', { navigationToken: bootstrapToken })
    const userOpen = openFile('canvas-b')

    router.settleBootstrap(bootstrapToken)
    expect(openFile).toHaveBeenCalledTimes(2)

    releaseUserB()
    await expect(userOpen).resolves.toBe(true)
    expect(location.hash).toBe('#/canvas/canvas-b')
    expect(openFile).toHaveBeenCalledTimes(2) // store publication did not re-enter router apply

    releaseInitialA()
    await expect(bootstrapOpen).resolves.toBe(false)
    expect(state.doc.id).toBe('canvas-b')

    history.back()
    await vi.waitFor(() => expect(state.doc.id).toBe('canvas-a'))
    history.forward()
    await vi.waitFor(() => expect(state.doc.id).toBe('canvas-b'))
    expect(openFile).toHaveBeenCalledTimes(4)
  })
})
