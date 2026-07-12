import { describe, expect, it } from 'vitest'
import type { CatalogTable } from '../types/api'
import { rankedResultFacets } from './CatalogView'

const table = (name: string, folder: string, tags: string[], owner: string): CatalogTable => ({
  id: `tbl_${name}`, name, uri: `mem://${name}`, columns: [], folder, tags, owner,
})

describe('rankedResultFacets', () => {
  it('describes the bounded meaning results instead of a lexical result set', () => {
    const facets = rankedResultFacets([
      table('a', 'finance', ['gold', 'gold', 'daily'], 'fin'),
      table('b', 'finance', ['gold'], 'fin'),
      table('c', 'archive', ['cold'], 'ops'),
    ])

    expect(facets.folders).toEqual([
      { value: 'finance', count: 2 }, { value: 'archive', count: 1 },
    ])
    expect(facets.tags).toEqual([
      { value: 'gold', count: 2 }, { value: 'cold', count: 1 }, { value: 'daily', count: 1 },
    ])
    expect(facets.owners).toEqual([
      { value: 'fin', count: 2 }, { value: 'ops', count: 1 },
    ])
    expect(facets.semanticAvailable).toBe(true)
  })
})
