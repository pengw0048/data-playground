import { exampleDoc } from './examples'
import {
  filterBuilderReason, isScalarNumericType, serializeFilterConditions,
} from './nodes/filterValidation'
import type { CatalogTable } from './types/api'
import type { CanvasDoc, ColumnSchema } from './types/graph'

export type CanvasStarter = { kind: 'example'; key: string } | {
  kind: 'numeric-filter'
  table: CatalogTable
  column: string
  threshold: string
}

export function numericStarterColumn(column: ColumnSchema): boolean {
  return isScalarNumericType(column.physicalType || column.type)
}

function condition(table: CatalogTable, column: string, threshold: string) {
  const schema = table.columns.find((candidate) => candidate.name === column)
  return { col: column, op: '>', val: threshold.trim(), type: schema?.physicalType || schema?.type }
}

export function numericFilterStarterReason(table: CatalogTable, column: string, threshold: string): string | null {
  if (table.missing) return 'This dataset is unavailable. Choose another dataset.'
  if (!table.registrationId || !table.id || !table.uri) return 'Choose a registered dataset.'
  const matches = table.columns.filter((candidate) => candidate.name === column)
  if (matches.length !== 1 || !numericStarterColumn(matches[0])) return 'Choose a numeric column.'
  return filterBuilderReason([condition(table, column, threshold)], table.columns)
}

/** A concrete two-step workflow; execution and result storage remain explicit Canvas actions. */
export function starterDoc(starter: CanvasStarter, id: string): CanvasDoc | null {
  if (starter.kind === 'example') return exampleDoc(starter.key, id)
  const { table, column, threshold } = starter
  const reason = numericFilterStarterReason(table, column, threshold)
  if (reason) throw new Error(reason)
  const conditions = [condition(table, column, threshold)]
  return {
    id, name: `Filter ${table.name}`, version: 1,
    nodes: [
      {
        id: 'src', type: 'source', position: { x: 80, y: 180 },
        data: {
          title: table.name, status: 'draft', config: {
            uri: table.uri, tableId: table.id, registrationId: table.registrationId!,
          },
        },
      },
      {
        id: 'flt', type: 'filter', position: { x: 400, y: 180 },
        data: {
          title: 'Filter rows', status: 'draft', config: {
            predicate: serializeFilterConditions(conditions, table.columns),
            filterBuilder: { conditions },
          },
        },
      },
    ],
    edges: [{
      id: 'e_src_flt', source: 'src', target: 'flt', sourceHandle: 'out', targetHandle: 'in',
      data: { wire: 'dataset' },
    }],
  }
}
