import type { ColumnSchema } from '../types/graph'

type FilterColumn = Pick<ColumnSchema, 'name'> & { type?: string }

export const FILTER_OPS = ['=', '!=', '>', '>=', '<', '<=', 'LIKE', 'IS NULL', 'IS NOT NULL'] as const
export type FilterOp = typeof FILTER_OPS[number]
export interface FilterCondition {
  col: string
  op: FilterOp | string
  val: string
  type?: string
}

const NULL_OPS = new Set<FilterOp>(['IS NULL', 'IS NOT NULL'])
const NUMBER = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/

export function parseFilterConditions(predicate: string): FilterCondition[] | null {
  const trimmed = predicate.trim()
  if (!trimmed) return []
  if (/\bor\b|\(|\)/i.test(trimmed)) return null
  const conditions: FilterCondition[] = []
  for (const part of trimmed.split(/\s+AND\s+/i)) {
    const nullMatch = part.match(/^(.+?)\s+(IS NOT NULL|IS NULL)$/i)
    if (nullMatch) {
      conditions.push({ col: nullMatch[1].trim(), op: nullMatch[2].toUpperCase(), val: '' })
      continue
    }
    const match = part.match(/^([A-Za-z_][\w.]*|"[^"]+")\s*(!=|>=|<=|=|>|<|LIKE)(?![=<>!])\s*(.+)$/i)
    if (!match || /^[=<>!]/.test(match[3].trim())) return null
    conditions.push({ col: match[1].trim(), op: match[2].toUpperCase(), val: match[3].trim() })
  }
  return conditions
}

export function filterBuilderConditions(config: Record<string, unknown>): FilterCondition[] | null {
  const builder = config.filterBuilder
  if (!builder || typeof builder !== 'object' || !Array.isArray((builder as { conditions?: unknown }).conditions)) return null
  return (builder as { conditions: unknown[] }).conditions.map((condition) => {
    const value = condition && typeof condition === 'object' ? condition as Record<string, unknown> : {}
    return {
      col: typeof value.col === 'string' ? value.col : '',
      op: typeof value.op === 'string' ? value.op : '',
      val: typeof value.val === 'string' ? value.val : '',
      type: typeof value.type === 'string' ? value.type : undefined,
    }
  })
}

function conditionType(condition: FilterCondition, columns?: FilterColumn[]): string | undefined {
  return columns?.find((column) => column.name === condition.col)?.type ?? condition.type
}

function numeric(type: string | undefined): boolean {
  return !!type && /int|decimal|numeric|double|float|real/i.test(type)
}

function boolean(type: string | undefined): boolean {
  return !!type && /bool/i.test(type)
}

export function filterBuilderReason(
  conditions: FilterCondition[], columns?: FilterColumn[],
): string | null {
  for (const condition of conditions) {
    const column = condition.col.trim()
    if (!column) return 'Choose a column'
    if (!FILTER_OPS.includes(condition.op as FilterOp)) return 'Choose an operator'
    if (NULL_OPS.has(condition.op as FilterOp)) continue
    const value = condition.val.trim()
    const type = conditionType(condition, columns)
    if (numeric(type) && (!NUMBER.test(value) || !Number.isFinite(Number(value)))) return `Enter a number for ${column}`
    if (boolean(type) && !/^(true|false)$/i.test(value)) return `Enter true or false for ${column}`
    if (!value) return `Enter a value for ${column}`
  }
  return null
}

function literal(value: string, type: string | undefined): string {
  const trimmed = value.trim()
  if (NUMBER.test(trimmed) || /^(true|false|null)$/i.test(trimmed) || /^'.*'$/.test(trimmed)) return trimmed
  const stringLike = !type || /string|json|struct|list|bytes|date|time|timestamp/i.test(type)
  return stringLike ? `'${trimmed.replace(/'/g, "''")}'` : trimmed
}

export function serializeFilterConditions(conditions: FilterCondition[], columns: FilterColumn[]): string {
  if (filterBuilderReason(conditions, columns)) return ''
  return conditions.map((condition) => {
    if (NULL_OPS.has(condition.op as FilterOp)) return `${condition.col.trim()} ${condition.op}`
    return `${condition.col.trim()} ${condition.op} ${literal(condition.val, conditionType(condition, columns))}`
  }).join(' AND ')
}
