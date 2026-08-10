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
      columnFilters: {},
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

  it('round-trips typed column filters and drops unknown or malformed entries', () => {
    expect(parseColumnFilterMap(
      'name:sales%20report,source:mount%3Alake,updated_after:2026-08-01,updated_before:2026-08-05',
    )).toEqual({
      name: 'sales report', source: 'mount:lake',
      updatedAfter: '2026-08-01', updatedBefore: '2026-08-05',
    })
    expect(parseColumnFilterMap('frame_id:12,score:high')).toBeUndefined()
    expect(parseColumnFilterMap('updated_after:not-a-date,source:elsewhere')).toBeUndefined()
    expect(parseColumnFilterMap('updated_after:2026-08-05,updated_before:2026-08-01')).toEqual({
      updatedAfter: '2026-08-01', updatedBefore: '2026-08-05',
    })

    const encoded = serializeBrowseState({
      sortMode: 'source', kindFilter: 'all', viewMode: 'list',
      columnFilters: { name: 'a, b: c', source: 'local' },
    })
    expect(browseStateFromQuery(encoded).columnFilters).toEqual({
      name: 'a, b: c', source: 'local',
    })
  })

  it('round-trips rows and owner filters and keeps the rows range ordered', () => {
    expect(parseColumnFilterMap('rows_min:1000,rows_max:90000,owner:peer-user')).toEqual({
      rowsMin: 1000, rowsMax: 90000, owner: 'peer-user',
    })
    expect(parseColumnFilterMap('rows_min:90000,rows_max:1000')).toEqual({
      rowsMin: 1000, rowsMax: 90000,
    })
    expect(parseColumnFilterMap('rows_min:-5,rows_max:abc')).toBeUndefined()
    expect(parseColumnFilterMap('rows_min:0')).toEqual({ rowsMin: 0 })

    const encoded = serializeBrowseState({
      sortMode: 'source', kindFilter: 'all', viewMode: 'list',
      columnFilters: { rowsMin: 0, rowsMax: 500, owner: 'peer-user' },
    })
    expect(browseStateFromQuery(encoded).columnFilters).toEqual({
      rowsMin: 0, rowsMax: 500, owner: 'peer-user',
    })
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
