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
} | {
  kind: 'group-count'
  table: CatalogTable
  column: string
}

export type OwnDataStarter = Exclude<CanvasStarter, { kind: 'example' }>

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

export function groupCountStarterReason(table: CatalogTable, column: string): string | null {
  if (table.missing) return 'This dataset is unavailable. Choose another dataset.'
  if (!table.registrationId || !table.id || !table.uri) return 'Choose a registered dataset.'
  if (table.columns.filter((candidate) => candidate.name === column).length !== 1) return 'Choose a grouping column.'
  return null
}

export function ownDataStarterReason(starter: OwnDataStarter): string | null {
  return starter.kind === 'group-count'
    ? groupCountStarterReason(starter.table, starter.column)
    : numericFilterStarterReason(starter.table, starter.column, starter.threshold)
}

/** A concrete two-step workflow; execution and result storage remain explicit Canvas actions. */
export function starterDoc(starter: CanvasStarter, id: string): CanvasDoc | null {
  if (starter.kind === 'example') return exampleDoc(starter.key, id)
  const { table, column } = starter
  const reason = ownDataStarterReason(starter)
  if (reason) throw new Error(reason)
  const counting = starter.kind === 'group-count'
  const conditions = counting ? [] : [condition(table, column, starter.threshold)]
  // Group keys are identifiers, including names containing quotes, commas, or SQL words.
  const groupBy = '"' + column.replaceAll('"', '""') + '"'
  const countName = column.toLowerCase() === 'row_count' ? 'row_count_2' : 'row_count'
  const stepId = counting ? 'agg' : 'flt'
  return {
    id, name: `${counting ? 'Count by group in' : 'Filter'} ${table.name}`, version: 1,
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
        id: stepId, type: counting ? 'aggregate' : 'filter', position: { x: 400, y: 180 },
        data: {
          title: counting ? 'Count rows by group' : 'Filter rows', status: 'draft', config: counting ? {
            groupBy, aggs: `count(*) AS ${countName}`,
          } : {
            predicate: serializeFilterConditions(conditions, table.columns),
            filterBuilder: { conditions },
          },
        },
      },
    ],
    edges: [{
      id: `e_src_${stepId}`, source: 'src', target: stepId, sourceHandle: 'out', targetHandle: 'in',
      data: { wire: 'dataset' },
    }],
  }
}
