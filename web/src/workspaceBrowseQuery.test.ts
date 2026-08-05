import { describe, expect, it } from 'vitest'
import {
  browseStateFromQuery,
  extractWorkspaceBrowseQuery,
  normalizeWorkspaceBrowseQuery,
  parseColumnFilterMap,
  parseWorkspaceBrowseQuery,
  serializeBrowseState,
  serializeWorkspaceBrowseQuery,
  WORKSPACE_BROWSE_QUERY_VERSION,
} from './workspaceBrowseQuery'

describe('workspaceBrowseQuery', () => {
  it('round-trips committed browse decisions and omits defaults', () => {
    const encoded = serializeBrowseState({
      sortMode: 'name-asc',
      kindFilter: 'canvas',
      viewMode: 'grid',
    })
    expect(encoded).toContain(`wq=${WORKSPACE_BROWSE_QUERY_VERSION}`)
    expect(encoded).toContain('sort=name')
    expect(encoded).toContain('order=asc')
    expect(encoded).toContain('kind=canvas')
    expect(encoded).toContain('view=grid')
    expect(browseStateFromQuery(encoded)).toEqual({
      sortMode: 'name-asc',
      kindFilter: 'canvas',
      viewMode: 'grid',
    })
    expect(serializeBrowseState({
      sortMode: 'source', kindFilter: 'all', viewMode: 'list',
    })).toBe('')

    const opened = serializeBrowseState({
      sortMode: 'opened-desc', kindFilter: 'all', viewMode: 'list',
    })
    expect(opened).toContain('sort=opened')
    expect(browseStateFromQuery(opened).sortMode).toBe('opened-desc')
  })

  it('falls back safely for unknown, duplicate, malformed, and oversized values', () => {
    expect(parseWorkspaceBrowseQuery('wq=9&sort=name&order=asc')).toEqual({
      version: WORKSPACE_BROWSE_QUERY_VERSION,
    })
    expect(parseWorkspaceBrowseQuery('sort=bogus&order=asc&kind=nope&view=tiles')).toEqual({
      version: WORKSPACE_BROWSE_QUERY_VERSION,
    })
    expect(parseWorkspaceBrowseQuery('sort=name')).toEqual({
      version: WORKSPACE_BROWSE_QUERY_VERSION,
    })
    expect(parseWorkspaceBrowseQuery('order=desc')).toEqual({
      version: WORKSPACE_BROWSE_QUERY_VERSION,
    })

    const duplicate = new URLSearchParams()
    duplicate.append('kind', 'canvas')
    duplicate.append('kind', 'dataset')
    expect(parseWorkspaceBrowseQuery(duplicate).kind).toBe('canvas')

    const huge = `cf=${'a'.repeat(2000)}:x`
    expect(parseWorkspaceBrowseQuery(huge)).toEqual({
      version: WORKSPACE_BROWSE_QUERY_VERSION,
    })
  })

  it('parses a bounded column-filter map and rejects unsafe keys', () => {
    expect(parseColumnFilterMap('frame_id:12,score:high')).toEqual({
      frame_id: '12', score: 'high',
    })
    expect(parseColumnFilterMap('../secret:1')).toBeUndefined()
    expect(parseColumnFilterMap('ok:1,bad/key:2')).toEqual({ ok: '1' })
    expect(Object.keys(parseColumnFilterMap(
      Array.from({ length: 20 }, (_, i) => `c${i}:v`).join(','),
    ) ?? {})).toHaveLength(16)
  })

  it('normalizes and extracts browse keys without leaking dataset-viewer keys', () => {
    const mixed = new URLSearchParams({
      wq: '1', sort: 'updated', order: 'desc', kind: 'dataset', view: 'grid',
      revision: 'rev-1', returnCanvas: 'c1', q: 'search',
    }).toString()
    expect(extractWorkspaceBrowseQuery(mixed)).toBe(
      serializeWorkspaceBrowseQuery({
        version: 1, sort: 'updated', order: 'desc', kind: 'dataset', view: 'grid',
      }),
    )
    expect(normalizeWorkspaceBrowseQuery({
      version: 1, sort: 'name', order: 'asc', view: 'list', kind: undefined,
    })).toEqual({ version: 1, sort: 'name', order: 'asc' })
  })
})
