import { describe, expect, it } from 'vitest'
import { groupCountStarterReason, starterDoc } from './starters'
import type { CatalogTable } from './types/api'

const table = (name: string): CatalogTable => ({
  id: 'data', registrationId: 'registered-data', name: 'Measurements', uri: '/data/measurements.parquet',
  columns: [{ name, type: 'string', capabilities: [] }],
})

describe('group count starter', () => {
  it.each(['kind, "label"', 'select', 'a.b'])('groups by the exact column %j', (column) => {
    const doc = starterDoc({ kind: 'group-count', table: table(column), column }, 'counts')!
    expect(doc.nodes[1].data.config).toEqual({
      groupBy: '"' + column.replaceAll('"', '""') + '"', aggs: 'count(*) AS row_count',
    })
    expect(doc.edges[0]).toMatchObject({ source: 'src', target: 'agg', sourceHandle: 'out', targetHandle: 'in' })
  })

  it('keeps a row_count grouping column distinct from the count output', () => {
    const doc = starterDoc({ kind: 'group-count', table: table('ROW_COUNT'), column: 'ROW_COUNT' }, 'counts')!
    expect(doc.nodes[1].data.config).toEqual({ groupBy: '"ROW_COUNT"', aggs: 'count(*) AS row_count_2' })
  })

  it('refuses missing data and ambiguous or missing column identities', () => {
    const source = table('category')
    expect(groupCountStarterReason({ ...source, missing: true }, 'category')).not.toBeNull()
    expect(groupCountStarterReason({ ...source, registrationId: undefined }, 'category')).not.toBeNull()
    expect(groupCountStarterReason(source, 'deleted')).not.toBeNull()
    expect(groupCountStarterReason({ ...source, columns: [...source.columns, ...source.columns] }, 'category')).not.toBeNull()
  })
})
