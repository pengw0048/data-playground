import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  canvasLink,
  datasetViewerHash,
  initRouter,
  parseDatasetViewerReturn,
  parseHash,
  relationshipsHash,
  resetRouterForTests,
  routeHash,
} from './router'
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

  it('round-trips committed Workspace browse projection without pagination cursors', () => {
    const browse = 'wq=1&sort=name&order=asc&kind=canvas&view=grid'
    window.location.hash = routeHash(
      'workspace', undefined, 'container:folder-1', 'robot', undefined, undefined, undefined,
      'all', undefined, undefined, undefined, undefined, undefined, undefined, browse,
    )
    expect(parseHash()).toEqual({
      view: 'workspace',
      workspaceResourceId: 'container:folder-1',
      workspaceQuery: 'robot',
      workspaceBrowseQuery: browse,
    })
    expect(window.location.hash).not.toMatch(/cursor=/i)
  })

  it('canonicalizes malformed browse projection values without breaking the route', () => {
    window.location.hash = '#/workspace?view=tiles&sort=bogus&order=asc&kind=canvas'
    expect(parseHash()).toEqual({
      view: 'workspace',
      workspaceBrowseQuery: 'wq=1&kind=canvas',
      canonicalHash: '#/workspace?wq=1&kind=canvas',
    })
  })

  it('preserves legacy Workspace URLs and keeps datasets-scope sort out of browse projection', () => {
    window.location.hash = '#/workspace'
    expect(parseHash()).toEqual({ view: 'workspace' })

    const datasetQuery = new URLSearchParams({
      dq: 'robot', sort: 'updated', order: 'desc',
    }).toString()
    window.location.hash = routeHash(
      'workspace', undefined, 'dataset:x', undefined, undefined, undefined, undefined,
      'datasets', datasetQuery,
    )
    expect(parseHash()).toEqual({
      view: 'workspace', workspaceResourceId: 'dataset:x', workspaceScope: 'datasets',
      workspaceDatasetQuery: datasetQuery,
    })
    expect(parseHash().workspaceBrowseQuery).toBeUndefined()
  })

  it('round-trips Relationships return browse projection', () => {
    window.location.hash = relationshipsHash({
      focusDatasetId: 'dataset:1',
      mode: 'lineage',
      returnTo: {
        resourceId: 'container:folder-1',
        scope: 'all',
        workspaceQuery: 'robot',
        browseQuery: 'wq=1&view=grid',
      },
    })
    expect(parseHash()).toEqual({
      view: 'relationships',
      relationshipsContext: {
        focusDatasetId: 'dataset:1',
        mode: 'lineage',
        returnTo: {
          resourceId: 'container:folder-1',
          scope: 'all',
          workspaceQuery: 'robot',
          browseQuery: 'wq=1&view=grid',
        },
      },
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

  it('preserves an exact receipt dataset identity alongside its revision', () => {
    const datasetQuery = new URLSearchParams({ revision: 'rev-9', revisionDataset: 'logical-receipt-id' }).toString()
    window.location.hash = routeHash('workspace', undefined, 'dataset:registration-current', undefined,
      undefined, undefined, undefined, 'datasets', datasetQuery)
    expect(parseHash()).toEqual({
      view: 'workspace', workspaceResourceId: 'dataset:registration-current', workspaceScope: 'datasets',
      workspaceDatasetQuery: datasetQuery,
    })
  })

  it('builds one dataset viewer route for latest and exact dataset identities', () => {
    window.location.hash = datasetViewerHash('dataset/with spaces')
    expect(parseHash()).toEqual({
      view: 'workspace',
      workspaceResourceId: 'dataset:dataset/with spaces',
    })

    window.location.hash = datasetViewerHash('dataset/with spaces', 'revision 9')
    expect(parseHash()).toEqual({
      view: 'workspace',
      workspaceResourceId: 'dataset:dataset/with spaces',
      workspaceDatasetQuery: new URLSearchParams({
        revision: 'revision 9',
        revisionDataset: 'dataset/with spaces',
      }).toString(),
    })

    window.location.hash = datasetViewerHash(
      'dataset/with spaces',
      'revision 9',
      { canvasId: 'canvas 1', nodeId: 'write 1' },
    )
    expect(parseHash()).toEqual({
      view: 'workspace',
      workspaceResourceId: 'dataset:dataset/with spaces',
      workspaceDatasetQuery: new URLSearchParams({
        revision: 'revision 9',
        revisionDataset: 'dataset/with spaces',
        returnCanvas: 'canvas 1',
        returnNode: 'write 1',
      }).toString(),
    })

    const jobsQuery = new URLSearchParams({ status: 'failed', run: 'run 1' }).toString()
    window.location.hash = datasetViewerHash(
      'dataset/with spaces',
      'revision 9',
      { view: 'jobs', query: jobsQuery },
    )
    const jobsRoute = parseHash()
    expect(jobsRoute).toEqual({
      view: 'workspace',
      workspaceResourceId: 'dataset:dataset/with spaces',
      workspaceDatasetQuery: new URLSearchParams({
        revision: 'revision 9',
        revisionDataset: 'dataset/with spaces',
        returnView: 'jobs',
        returnQuery: jobsQuery,
      }).toString(),
    })
    expect(parseDatasetViewerReturn(jobsRoute.workspaceDatasetQuery ?? '')).toEqual({
      view: 'jobs', query: jobsQuery,
    })
    expect(parseDatasetViewerReturn(new URLSearchParams({
      returnView: 'inbox', returnQuery: 'filter=unread&next=https%3A%2F%2Fevil.example',
    }).toString())).toEqual({ view: 'inbox', query: 'filter=unread' })
  })

  it('opens a provider exact dataset at its Workspace placement without projecting it into the local catalog', () => {
    window.location.hash = datasetViewerHash(
      'workspace-provider:canonical-source',
      'provider revision 9',
      { canvasId: 'canvas 1', nodeId: 'source 1' },
      'dataset:external/provider-placement',
    )

    expect(window.location.hash).not.toContain('scope=datasets')
    expect(parseHash()).toEqual({
      view: 'workspace',
      workspaceResourceId: 'dataset:external/provider-placement',
      workspaceDatasetQuery: new URLSearchParams({
        revision: 'provider revision 9',
        revisionDataset: 'workspace-provider:canonical-source',
        returnCanvas: 'canvas 1',
        returnNode: 'source 1',
      }).toString(),
    })
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

  it('builds canonical list routes without stale unavailable detail identities', () => {
    window.location.hash = routeHash('jobs')
    expect(window.location.hash).toBe('#/jobs')
    expect(parseHash()).toEqual({ view: 'jobs', jobsQuery: '' })

    window.location.hash = routeHash('transforms')
    expect(window.location.hash).toBe('#/transforms')
    expect(parseHash()).toEqual({ view: 'transforms' })
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

  it('round-trips focused lineage and its exact Dataset return without changing the global route', () => {
    const datasetQuery = new URLSearchParams({
      revision: 'revision 9',
      revisionDataset: 'logical dataset',
      returnCanvas: 'canvas 1',
      ignored: 'must-not-return',
    }).toString()
    window.location.hash = relationshipsHash({
      focusDatasetId: 'stable registration',
      mode: 'lineage',
      returnTo: {
        resourceId: 'dataset:stable registration',
        scope: 'datasets',
        datasetQuery,
      },
    })

    expect(parseHash()).toEqual({
      view: 'relationships',
      relationshipsContext: {
        focusDatasetId: 'stable registration',
        mode: 'lineage',
        returnTo: {
          resourceId: 'dataset:stable registration',
          scope: 'datasets',
          datasetQuery: new URLSearchParams({
            revision: 'revision 9',
            revisionDataset: 'logical dataset',
            returnCanvas: 'canvas 1',
          }).toString(),
        },
      },
    })

    expect(relationshipsHash()).toBe('#/relationships')
    window.location.hash = routeHash('relationships')
    expect(parseHash()).toEqual({ view: 'relationships' })
  })

  it('round-trips a canvas node deep link', () => {
    window.location.hash = routeHash('canvas', 'canvas-1', undefined, undefined, undefined, 'write-1')
    expect(parseHash()).toEqual({ view: 'canvas', canvasId: 'canvas-1', nodeId: 'write-1' })
  })

  it('accepts an optional decorative title slug without using it for lookup', () => {
    window.location.hash = routeHash(
      'canvas', 'file-key-1', undefined, undefined, undefined, 'node-1',
      undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
      undefined, '销售分析 Draft',
    )
    expect(window.location.hash).toBe(
      `#/canvas/file-key-1/${encodeURIComponent('销售分析-Draft')}?node=node-1`,
    )
    expect(parseHash()).toEqual({ view: 'canvas', canvasId: 'file-key-1', nodeId: 'node-1' })

    window.location.hash = '#/canvas/legacy_short/wrong-slug-for-another-title?node=step'
    expect(parseHash()).toEqual({ view: 'canvas', canvasId: 'legacy_short', nodeId: 'step' })

    window.location.hash = '#/canvas/abcdef123456'
    expect(parseHash()).toEqual({ view: 'canvas', canvasId: 'abcdef123456' })

    // Malformed slug encoding must not hide a valid file key.
    window.location.hash = '#/canvas/good-key/%E0%A4%A'
    expect(parseHash()).toEqual({ view: 'canvas', canvasId: 'good-key' })
  })

  it('omits the slug for untitled canvases and keeps share links keyed by file identity', () => {
    expect(routeHash('canvas', 'id-1', undefined, undefined, undefined, undefined,
      undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined,
      undefined, 'untitled'))
      .toBe('#/canvas/id-1')
    expect(canvasLink('id-1', 'Purchases per user')).toBe(
      `${location.origin}${location.pathname}#/canvas/id-1/Purchases-per-user`,
    )
  })

  it.each([
    ['#/canvas/%E0%A4%A', { view: 'workspace', canonicalHash: '#/workspace' }],
    ['#/workspace/%E0%A4%A', { view: 'workspace', canonicalHash: '#/workspace' }],
    ['#/transforms/%E0%A4%A', { view: 'transforms', canonicalHash: '#/transforms' }],
    ['#/distribution-reports/%E0%A4%A', { view: 'workspace', canonicalHash: '#/workspace' }],
  ])('keeps malformed encoded route identifiers inside routing for %s', (hash, expected) => {
    window.location.hash = hash
    expect(() => parseHash()).not.toThrow()
    expect(parseHash()).toEqual(expected)
  })

  it('reserves last-Canvas recovery for the bare route', () => {
    window.location.hash = '#/'
    expect(parseHash()).toEqual({ view: 'canvas' })

    for (const hash of ['#/not-a-route', '#//not-a-route', '#///also-invalid', '#/canvas', '#/?unexpected=true']) {
      window.location.hash = hash
      expect(parseHash()).toEqual({ view: 'workspace', canonicalHash: '#/workspace' })
    }
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

  it('pushes distinct Workspace browse decisions and restores them through Back/Forward', async () => {
    const state = {
      view: 'workspace' as DpView,
      doc: { id: 'canvas-current', nodes: [] as { id: string }[] }, selectedId: null as string | null,
      workspaceResourceId: null as string | null,
      workspaceSearchQuery: '',
      workspaceScope: 'all' as const,
      workspaceDatasetQuery: '',
      workspaceBrowseQuery: '',
      jobsQuery: '', inboxQuery: '', transformResourceId: null as string | null,
      transformVersion: null as string | null, transformUpgradeCanvasId: null as string | null,
      transformUpgradeNodeId: null as string | null, transformLibraryQuery: '',
    }
    const subscribers = new Set<(snapshot: typeof state) => void>()
    const publish = () => { for (const subscriber of subscribers) subscriber(state) }
    const store = {
      getState: () => ({
        ...state,
        applyRoute: (route: {
          view: DpView
          workspaceResourceId?: string
          workspaceQuery?: string
          workspaceBrowseQuery?: string
          workspaceDatasetQuery?: string
          workspaceScope?: 'all' | 'datasets'
        }) => {
          if (route.view !== 'workspace') {
            state.view = route.view
            publish()
            return
          }
          state.view = 'workspace'
          state.workspaceResourceId = route.workspaceResourceId ?? null
          state.workspaceSearchQuery = route.workspaceQuery ?? ''
          state.workspaceBrowseQuery = route.workspaceBrowseQuery ?? ''
          state.workspaceDatasetQuery = route.workspaceDatasetQuery ?? ''
          state.workspaceScope = route.workspaceScope ?? 'all'
          publish()
        },
        select: vi.fn(), requestNodeReveal: vi.fn(), clearNodeReveal: vi.fn(),
        requestViewportFit: vi.fn(), pushToast: vi.fn(), openFile: vi.fn(async () => false),
      }),
      subscribe: (subscriber: (snapshot: typeof state) => void) => {
        subscribers.add(subscriber)
        return () => { subscribers.delete(subscriber) }
      },
    }
    history.replaceState(null, '', '#/workspace')
    const bootstrapToken = startNavigation()
    const router = initRouter(store, bootstrapToken)
    router.settleBootstrap(bootstrapToken)

    state.workspaceBrowseQuery = 'wq=1&view=grid'
    publish()
    await vi.waitFor(() => expect(location.hash).toBe('#/workspace?wq=1&view=grid'))

    state.workspaceBrowseQuery = 'wq=1&sort=name&order=asc&view=grid'
    publish()
    await vi.waitFor(() => expect(location.hash).toBe('#/workspace?wq=1&sort=name&order=asc&view=grid'))

    history.back()
    await vi.waitFor(() => {
      expect(state.workspaceBrowseQuery).toBe('wq=1&view=grid')
      expect(location.hash).toBe('#/workspace?wq=1&view=grid')
    })
    history.forward()
    await vi.waitFor(() => {
      expect(state.workspaceBrowseQuery).toBe('wq=1&sort=name&order=asc&view=grid')
      expect(location.hash).toBe('#/workspace?wq=1&sort=name&order=asc&view=grid')
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
      workspaceDatasetQuery: '', workspaceBrowseQuery: '',
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
        requestViewportFit: vi.fn(),
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
      navigationToken: expect.any(Number), skipViewportFit: true,
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
      workspaceDatasetQuery: '', workspaceBrowseQuery: '', jobsQuery: '', inboxQuery: '', transformResourceId: null,
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
        requestNodeReveal: vi.fn(), clearNodeReveal: vi.fn(), requestViewportFit: vi.fn(), pushToast: vi.fn(), openFile,
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

  it('requests a one-shot overview when a normal Canvas route returns from Jobs to the loaded document', async () => {
    const state = {
      view: 'jobs' as DpView,
      doc: { id: 'canvas-current', nodes: [{ id: 'step-1' }] }, selectedId: null as string | null,
      workspaceResourceId: null, workspaceSearchQuery: '', workspaceScope: 'all' as const,
      workspaceDatasetQuery: '', workspaceBrowseQuery: '', jobsQuery: '', inboxQuery: '', transformResourceId: null,
      transformVersion: null, transformUpgradeCanvasId: null, transformUpgradeNodeId: null,
      transformLibraryQuery: '',
    }
    const requestViewportFit = vi.fn()
    const store = {
      getState: () => ({
        ...state,
        applyRoute: (route: { view: DpView }) => { state.view = route.view },
        select: vi.fn(), requestNodeReveal: vi.fn(), clearNodeReveal: vi.fn(), requestViewportFit,
        pushToast: vi.fn(), openFile: vi.fn(async () => false),
      }),
      subscribe: vi.fn(() => () => {}),
    }
    const bootstrapToken = startNavigation()
    const router = initRouter(store, bootstrapToken)
    router.settleBootstrap(bootstrapToken)

    history.replaceState(null, '', '#/canvas/canvas-current')
    window.dispatchEvent(new HashChangeEvent('hashchange'))

    await vi.waitFor(() => expect(state.view).toBe('canvas'))
    expect(requestViewportFit).toHaveBeenCalledOnce()
  })

  it('coalesces one history traversal and removes both listeners plus the store subscription', async () => {
    const state = {
      view: 'canvas' as DpView,
      doc: { id: 'canvas-current', nodes: [] as { id: string }[] }, selectedId: null as string | null,
      workspaceResourceId: null, workspaceSearchQuery: '', workspaceScope: 'all' as const,
      workspaceDatasetQuery: '', workspaceBrowseQuery: '', jobsQuery: '', inboxQuery: '', transformResourceId: null,
      transformVersion: null, transformUpgradeCanvasId: null, transformUpgradeNodeId: null,
      transformLibraryQuery: '',
    }
    const openFile = vi.fn(async () => false)
    const unsubscribe = vi.fn()
    const store = {
      getState: () => ({
        ...state,
        applyRoute: vi.fn(), select: vi.fn(), requestNodeReveal: vi.fn(), clearNodeReveal: vi.fn(), requestViewportFit: vi.fn(),
        pushToast: vi.fn(), openFile,
      }),
      subscribe: vi.fn(() => unsubscribe),
    }
    const addListener = vi.spyOn(window, 'addEventListener')
    const removeListener = vi.spyOn(window, 'removeEventListener')
    const bootstrapToken = startNavigation()
    const router = initRouter(store, bootstrapToken)
    router.settleBootstrap(bootstrapToken)
    history.replaceState(null, '', '#/canvas/canvas-history')

    window.dispatchEvent(new PopStateEvent('popstate'))
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await vi.waitFor(() => expect(openFile).toHaveBeenCalledTimes(1))

    const hashListener = addListener.mock.calls.find(([type]) => type === 'hashchange')?.[1]
    const popListener = addListener.mock.calls.find(([type]) => type === 'popstate')?.[1]
    resetRouterForTests()
    expect(unsubscribe).toHaveBeenCalledTimes(1)
    expect(removeListener).toHaveBeenCalledWith('hashchange', hashListener)
    expect(removeListener).toHaveBeenCalledWith('popstate', popListener)
  })

  it('canonicalizes missing or wrong title slugs without a history entry and keeps node focus', async () => {
    const state = {
      view: 'canvas' as DpView,
      doc: { id: 'file-key', name: 'Sales Analysis', nodes: [{ id: 'write-1' }] },
      selectedId: null as string | null,
      workspaceResourceId: null, workspaceSearchQuery: '', workspaceScope: 'all' as const,
      workspaceDatasetQuery: '', jobsQuery: '', inboxQuery: '', transformResourceId: null,
      transformVersion: null, transformUpgradeCanvasId: null, transformUpgradeNodeId: null,
      transformLibraryQuery: '',
      nodeRevealRequest: null as { canvasId: string; nodeId: string } | null,
    }
    const store = {
      getState: () => ({
        ...state,
        applyRoute: (route: { view: DpView }) => { state.view = route.view },
        select: (id: string | null) => { state.selectedId = id },
        requestNodeReveal: (canvasId: string, nodeId: string) => {
          state.nodeRevealRequest = { canvasId, nodeId }
        },
        clearNodeReveal: () => { state.nodeRevealRequest = null },
        requestViewportFit: vi.fn(), pushToast: vi.fn(),
        openFile: vi.fn(async () => true),
      }),
      subscribe: vi.fn(() => () => {}),
    }
    const bootstrapToken = startNavigation()
    const router = initRouter(store, bootstrapToken)
    router.settleBootstrap(bootstrapToken)
    const before = history.length

    history.replaceState(null, '', '#/canvas/file-key/wrong-title?node=write-1')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await vi.waitFor(() => expect(location.hash).toBe('#/canvas/file-key/Sales-Analysis?node=write-1'))
    expect(state.selectedId).toBe('write-1')
    expect(state.nodeRevealRequest).toEqual({ canvasId: 'file-key', nodeId: 'write-1' })
    expect(history.length).toBe(before)

    history.replaceState(null, '', '#/canvas/file-key?node=write-1')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await vi.waitFor(() => expect(location.hash).toBe('#/canvas/file-key/Sales-Analysis?node=write-1'))
    expect(history.length).toBe(before)
  })

  it('replaces history on rename slug changes and pushes when switching Canvas file keys', async () => {
    const state = {
      view: 'canvas' as DpView,
      doc: { id: 'canvas-a', name: 'Alpha', nodes: [] as { id: string }[] },
      selectedId: null as string | null,
      workspaceResourceId: null, workspaceSearchQuery: '', workspaceScope: 'all' as const,
      workspaceDatasetQuery: '', jobsQuery: '', inboxQuery: '', transformResourceId: null,
      transformVersion: null, transformUpgradeCanvasId: null, transformUpgradeNodeId: null,
      transformLibraryQuery: '',
    }
    const subscribers = new Set<(snapshot: typeof state) => void>()
    const publish = () => { for (const subscriber of subscribers) subscriber({ ...state }) }
    const store = {
      getState: () => ({
        ...state,
        applyRoute: vi.fn(), select: vi.fn(), requestNodeReveal: vi.fn(), clearNodeReveal: vi.fn(),
        requestViewportFit: vi.fn(), pushToast: vi.fn(), openFile: vi.fn(async () => true),
      }),
      subscribe: (subscriber: (snapshot: typeof state) => void) => {
        subscribers.add(subscriber)
        return () => { subscribers.delete(subscriber) }
      },
    }
    history.replaceState(null, '', '#/canvas/canvas-a/Alpha')
    const bootstrapToken = startNavigation()
    const router = initRouter(store, bootstrapToken)
    router.settleBootstrap(bootstrapToken)
    const lengthAfterSettle = history.length

    state.doc = { ...state.doc, name: 'Beta Draft' }
    publish()
    expect(location.hash).toBe('#/canvas/canvas-a/Beta-Draft')
    expect(history.length).toBe(lengthAfterSettle)

    state.doc = { id: 'canvas-b', name: 'Other', nodes: [] }
    publish()
    expect(location.hash).toBe('#/canvas/canvas-b/Other')
    expect(history.length).toBe(lengthAfterSettle + 1)
  })
})
