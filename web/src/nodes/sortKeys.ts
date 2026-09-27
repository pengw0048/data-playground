/** Only column ordering belongs in the builder. Expressions stay untouched in the raw editor. */
export interface SortKey {
  col: string
  dir: 'ASC' | 'DESC'
  nulls?: 'FIRST' | 'LAST'
}

// DuckDB accepts these bare terms as expressions (ALL means every output column), not names.
// Quoted versions remain ordinary columns; common names such as date/month are not excluded.
const BARE_EXPRESSIONS = new Set([
  'ALL', 'NULL', 'TRUE', 'FALSE', 'CURRENT_DATE', 'CURRENT_TIME', 'CURRENT_TIMESTAMP',
  'LOCALTIME', 'LOCALTIMESTAMP', 'CURRENT_USER', 'SESSION_USER', 'CURRENT_SCHEMA',
  'CURRENT_CATALOG', 'CURRENT_ROLE', 'USER',
])

export function parseSortKeys(by: string): SortKey[] | null {
  const value = by.trim()
  if (!value) return []
  const keys: SortKey[] = []
  // Consume a whole quoted identifier before looking for commas or ordering keywords.
  const item = /\s*(?:"((?:""|[^"])*)"|([A-Za-z_][A-Za-z0-9_$]*))(?:\s+(ASC|DESC))?(?:\s+NULLS\s+(FIRST|LAST))?\s*(,|$)/iy
  while (item.lastIndex < value.length) {
    const match = item.exec(value)
    if (!match) return null
    if (match[2] && BARE_EXPRESSIONS.has(match[2].toUpperCase())) return null
    const col = match[1] != null ? match[1].replaceAll('""', '"') : match[2]
    if (!col) return null
    keys.push({
      col, dir: (match[3]?.toUpperCase() ?? 'ASC') as SortKey['dir'],
      ...(match[4] ? { nulls: match[4].toUpperCase() as SortKey['nulls'] } : {}),
    })
    if (match[5] === ',' && item.lastIndex === value.length) return null
  }
  return keys
}

export function serializeSortKeys(keys: SortKey[]): string {
  return keys.filter((key) => key.col.length > 0).map((key) => (
    `"${key.col.replaceAll('"', '""')}" ${key.dir}${key.nulls ? ` NULLS ${key.nulls}` : ''}`
  )).join(', ')
}
