import { afterEach, describe, expect, it } from 'vitest'
import { parseHash, routeHash } from './router'

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

  it('round-trips Inbox filter query', () => {
    const query = new URLSearchParams({ filter: 'unread' }).toString()
    window.location.hash = routeHash('inbox', undefined, undefined, undefined, undefined, undefined, query)
    expect(parseHash()).toEqual({ view: 'inbox', inboxQuery: query })
  })

  it('round-trips a canvas node deep link', () => {
    window.location.hash = routeHash('canvas', 'canvas-1', undefined, undefined, undefined, 'write-1')
    expect(parseHash()).toEqual({ view: 'canvas', canvasId: 'canvas-1', nodeId: 'write-1' })
  })
})
