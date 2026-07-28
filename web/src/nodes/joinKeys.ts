// The join engine accepts either a same-name USING key list (`on`) or an ON predicate
// (`condition`).  Keep the UI's structured form deliberately narrower than SQL: it only
// represents ordered `a.left = b.right` equality pairs and leaves every other predicate raw.
export interface JoinKeyPair {
  left: string
  right: string
}

const IDENTIFIER = /^(?:"((?:""|[^"])*)"|([A-Za-z_][A-Za-z0-9_]*))$/
const EQUALITY = /^a\.(?:"((?:""|[^"])*)"|([A-Za-z_][A-Za-z0-9_]*))\s*=\s*b\.(?:"((?:""|[^"])*)"|([A-Za-z_][A-Za-z0-9_]*))$/i

function identifier(value: string): string | null {
  const match = value.trim().match(IDENTIFIER)
  if (!match) return null
  return (match[1] ?? match[2]).replaceAll('""', '"')
}

/** Parse the legacy same-name USING key list without guessing at expressions. */
export function parseJoinOn(on: string): JoinKeyPair[] | null {
  const value = on.trim()
  if (!value) return []
  const columns = value.split(',').map(identifier)
  if (columns.some((column) => column === null)) return null
  return (columns as string[]).map((column) => ({ left: column, right: column }))
}

/** Parse only the exact equality shape the key-pair builder can round-trip faithfully. */
export function parseJoinCondition(condition: string): JoinKeyPair[] | null {
  const value = condition.trim()
  if (!value) return []
  const pairs: JoinKeyPair[] = []
  for (const part of value.split(/\s+AND\s+/i)) {
    const match = part.trim().match(EQUALITY)
    if (!match) return null
    pairs.push({
      left: (match[1] ?? match[2]).replaceAll('""', '"'),
      right: (match[3] ?? match[4]).replaceAll('""', '"'),
    })
  }
  return pairs.length ? pairs : null
}

/** The raw condition wins in the backend, so never let an obsolete `on` hide it in the UI. */
export function parseJoinKeys(on: string, condition: string): JoinKeyPair[] | null {
  return condition.trim() ? parseJoinCondition(condition) : parseJoinOn(on)
}

function quoteIdentifier(column: string): string {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(column) ? column : `"${column.replaceAll('"', '""')}"`
}

/** Serialize to the existing backend contract, preserving pair order. */
export function serializeJoinKeys(pairs: JoinKeyPair[]): { on: string; condition: string } {
  const complete = pairs.filter((pair) => pair.left.trim() && pair.right.trim())
  if (complete.length && complete.every((pair) => pair.left === pair.right)) {
    return { on: complete.map((pair) => quoteIdentifier(pair.left)).join(', '), condition: '' }
  }
  return {
    on: '',
    condition: complete.map((pair) => `a.${quoteIdentifier(pair.left)} = b.${quoteIdentifier(pair.right)}`).join(' AND '),
  }
}
