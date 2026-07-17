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

  it('round-trips a canvas node deep link', () => {
    window.location.hash = routeHash('canvas', 'canvas-1', undefined, undefined, undefined, 'write-1')
    expect(parseHash()).toEqual({ view: 'canvas', canvasId: 'canvas-1', nodeId: 'write-1' })
  })
})
