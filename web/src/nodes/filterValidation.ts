import type { ColumnSchema } from '../types/graph'

type FilterColumn = Pick<ColumnSchema, 'name'> & { type?: string; physicalType?: string | null }

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
const IDENTIFIER = '(?:[A-Za-z_][\\w.]*|"(?:""|[^"])+")'
const COMPARISON = new RegExp(`^(${IDENTIFIER})\\s*(!=|>=|<=|=|>|<|LIKE)(?![=<>!])\\s*(.+)$`, 'i')
const NULL_COMPARISON = new RegExp(`^(${IDENTIFIER})\\s+(IS NOT NULL|IS NULL)$`, 'i')
const QUOTED_IDENTIFIER = /^"((?:""|[^"])*)"$/

/** Match one complete scalar type, never a numeric member of a list, struct, or map. */
export function isScalarNumericType(type: string | undefined): boolean {
  return !!type && /^(?:u?(?:tinyint|smallint|integer|bigint|hugeint)|u?int(?:8|16|32|64|128)?|float(?:16|32|64)?|double(?:\s+precision)?|real|(?:decimal(?:32|64|128|256)?|numeric)(?:\s*\(\s*\d+(?:\s*,\s*-?\d+)?\s*\))?)$/i.test(type.trim())
}

function simpleConditions(predicate: string): string[] | null {
  // Mask quoted text before recognizing conjunctions. SQL words inside a column or value are data.
  let outside = ''
  let quote = ''
  for (let index = 0; index < predicate.length; index += 1) {
    const char = predicate[index]
    if (quote) {
      outside += '_'
      if (char === quote) {
        if (predicate[index + 1] === quote) { outside += '_'; index += 1 }
        else quote = ''
      }
    } else if (char === '"' || char === "'") {
      quote = char
      outside += '_'
    } else outside += char
  }
  if (quote || /\bor\b|\(|\)/i.test(outside)) return null
  const parts: string[] = []
  let start = 0
  for (const match of outside.matchAll(/\s+AND\s+/gi)) {
    parts.push(predicate.slice(start, match.index))
    start = match.index + match[0].length
  }
  parts.push(predicate.slice(start))
  return parts
}

export function parseFilterConditions(predicate: string): FilterCondition[] | null {
  const trimmed = predicate.trim()
  if (!trimmed) return []
  const parts = simpleConditions(trimmed)
  if (!parts) return null
  const conditions: FilterCondition[] = []
  for (const part of parts) {
    const nullMatch = part.match(NULL_COMPARISON)
    if (nullMatch) {
      conditions.push({ col: nullMatch[1].trim(), op: nullMatch[2].toUpperCase(), val: '' })
      continue
    }
    const match = part.match(COMPARISON)
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

function schemaColumn(condition: FilterCondition, columns?: FilterColumn[]): FilterColumn | undefined {
  const exact = columns?.find((column) => column.name === condition.col)
  if (exact) return exact
  const quoted = condition.col.trim().match(QUOTED_IDENTIFIER)
  return quoted ? columns?.find((column) => column.name === quoted[1].replaceAll('""', '"')) : undefined
}

function conditionType(condition: FilterCondition, columns?: FilterColumn[]): string | undefined {
  const column = schemaColumn(condition, columns)
  return column?.physicalType ?? column?.type ?? condition.type
}

function boolean(type: string | undefined): boolean {
  return !!type && /bool/i.test(type)
}

function exactNumericType(type: string | undefined): boolean {
  return isScalarNumericType(type) && !/^(?:float|double|real)/i.test(type!.trim())
}

function numericPrecision(value: string): { precision: number; scale: number; cast: boolean } | null {
  const [mantissa, exponentText] = value.replace(/^[+-]/, '').toLowerCase().split('e')
  // Only the bounded decimal-point displacement is numeric JS state; the threshold stays text.
  if (exponentText && exponentText.replace(/^[+-]?0*/, '').length > 2) return null
  const exponent = exponentText ? Number(exponentText) : 0
  const [integer, fraction = ''] = mantissa.split('.')
  const leadingZeros = (integer + fraction).match(/^0*/)![0].length
  const scale = Math.max(0, fraction.length - exponent)
  const integerDigits = Math.max(0, integer.length + exponent - leadingZeros)
  return {
    precision: Math.max(1, integerDigits + scale),
    scale,
    cast: exponentText !== undefined || integer.length + fraction.length > 38,
  }
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
    if (isScalarNumericType(type)) {
      if (!NUMBER.test(value)) return `Enter a number for ${column}`
      if (exactNumericType(type)) {
        const numeric = numericPrecision(value)
        if (!numeric || numeric.precision > 38) return `Use at most 38 digits for ${column}, including decimal places`
      } else if (!Number.isFinite(Number(value))) return `Enter a number for ${column}`
    }
    if (boolean(type) && !/^(true|false)$/i.test(value)) return `Enter true or false for ${column}`
    if (!value) return `Enter a value for ${column}`
  }
  return null
}

function literal(value: string, type: string | undefined): string {
  const trimmed = value.trim()
  if (exactNumericType(type)) {
    const { precision, scale, cast } = numericPrecision(trimmed)!
    // DuckDB counts written leading zeros when typing decimal literals. An explicit cast preserves
    // a 38-place fraction or scientific notation that would otherwise silently become DOUBLE.
    return cast ? `CAST('${trimmed}' AS DECIMAL(${precision},${scale}))` : trimmed
  }
  if (NUMBER.test(trimmed) || /^(true|false|null)$/i.test(trimmed) || /^'.*'$/.test(trimmed)) return trimmed
  const stringLike = !type || /string|json|struct|list|bytes|date|time|timestamp/i.test(type)
  return stringLike ? `'${trimmed.replace(/'/g, "''")}'` : trimmed
}

export function serializeFilterConditions(conditions: FilterCondition[], columns: FilterColumn[]): string {
  if (filterBuilderReason(conditions, columns)) return ''
  return conditions.map((condition) => {
    const known = schemaColumn(condition, columns)
    // A schema-backed choice is one literal column name, including dots and SQL keywords. Keep the
    // existing free-expression path for manually entered expressions that are not schema columns.
    const column = known ? `"${known.name.replaceAll('"', '""')}"` : condition.col.trim()
    if (NULL_OPS.has(condition.op as FilterOp)) return `${column} ${condition.op}`
    return `${column} ${condition.op} ${literal(condition.val, conditionType(condition, columns))}`
  }).join(' AND ')
}
