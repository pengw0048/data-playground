import type { ColumnSchema } from '../types/graph'

export const EDITOR_EXAMPLE_MAX_ROWS = 20
export const EDITOR_EXAMPLE_MAX_BYTES = 16 * 1024

export type EditorExampleRowsValidation =
  | {
      ok: true
      rows: Record<string, unknown>[]
      rowCount: number
      fields: string[]
      bytes: number
    }
  | { ok: false; error: string; bytes: number }

export function validateEditorExampleRows(value: string): EditorExampleRowsValidation {
  const bytes = new TextEncoder().encode(value).byteLength
  if (bytes > EDITOR_EXAMPLE_MAX_BYTES) {
    return {
      ok: false,
      error: `Example rows must be at most ${EDITOR_EXAMPLE_MAX_BYTES.toLocaleString()} UTF-8 bytes.`,
      bytes,
    }
  }
  let document: unknown
  try {
    document = JSON.parse(value)
  } catch {
    return { ok: false, error: 'Enter a valid JSON array of objects.', bytes }
  }
  if (!Array.isArray(document)) {
    return { ok: false, error: 'Example rows must be a JSON array of objects.', bytes }
  }
  if (document.length === 0) {
    return { ok: false, error: 'Add at least one example row.', bytes }
  }
  if (document.length > EDITOR_EXAMPLE_MAX_ROWS) {
    return {
      ok: false,
      error: `Example rows may contain at most ${EDITOR_EXAMPLE_MAX_ROWS} rows.`,
      bytes,
    }
  }
  if (document.some((row) => (
    row == null || typeof row !== 'object' || Array.isArray(row)
  ))) {
    return { ok: false, error: 'Every example row must be a JSON object.', bytes }
  }
  const rows = document as Record<string, unknown>[]
  const fields = Object.keys(rows[0] ?? {}).sort()
  if (fields.length === 0) {
    return { ok: false, error: 'Example rows must contain at least one field.', bytes }
  }
  if (rows.slice(1).some((row) => {
    const candidate = Object.keys(row).sort()
    return candidate.length !== fields.length
      || candidate.some((field, index) => field !== fields[index])
  })) {
    return { ok: false, error: 'Every example row must use the same fields.', bytes }
  }
  return { ok: true, rows, rowCount: rows.length, fields, bytes }
}

function starterValue(column: ColumnSchema): unknown {
  const type = String(column.physicalType || column.type || '').toLowerCase()
  if (type.includes('bool')) return true
  if (/(^|[^a-z])(u?int|integer|hugeint|smallint|tinyint)/.test(type)) return 1
  if (/(float|double|real|decimal|numeric)/.test(type)) return 1.5
  if (/(list|array)/.test(type)) return []
  if (/(struct|map|json)/.test(type)) return { example: 'value' }
  if (type.includes('timestamp') || type.includes('datetime')) return '2026-01-01T00:00:00Z'
  if (type.includes('date')) return '2026-01-01'
  return 'example'
}

export function editorExampleRowsStarter(columns: ColumnSchema[]): string {
  const known = columns.slice(0, 12)
  const row = known.length
    ? Object.fromEntries(known.map((column) => [column.name, starterValue(column)]))
    : { value: 1 }
  return JSON.stringify([row], null, 2)
}
