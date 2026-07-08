// Schema propagation for the editor (typed vs untyped ports). The kernel returns per-node OUTPUT
// columns where it can resolve them cheaply (relational ops); a code op / section is `null` here
// (untyped) until it has actually run, at which point we fall back to its last observed preview.
import type { CanvasDoc, CanvasNode, ColumnSchema } from '../types/graph'
import type { CatalogTable, SampleResult } from '../types/api'

export type SchemaMap = Record<string, ColumnSchema[] | null>
export type PreviewMap = Record<string, { result?: SampleResult; loading?: boolean; error?: string }>

/** OUTPUT columns of a node: server schema (typed) → last observed preview → source catalog. */
export function nodeColumns(
  doc: CanvasDoc, schemas: SchemaMap, previews: PreviewMap, catalog: CatalogTable[], id: string,
): ColumnSchema[] {
  const s = schemas[id]
  if (s && s.length) return s
  const pv = previews[id]?.result?.columns
  if (pv && pv.length) return pv as ColumnSchema[]
  const n = doc.nodes.find((x) => x.id === id)
  if (n?.type === 'source' && n.data.config.uri) {
    const t = catalog.find((c) => c.uri === n.data.config.uri)
    if (t?.columns?.length) return t.columns as ColumnSchema[]
  }
  return []
}

/** INPUT columns available to a node = the OUTPUT columns of its upstream(s), de-duped by name. */
export function inputColumns(
  doc: CanvasDoc, schemas: SchemaMap, previews: PreviewMap, catalog: CatalogTable[], id: string,
): ColumnSchema[] {
  const sources = doc.edges.filter((e) => e.target === id).map((e) => e.source)
  const out: ColumnSchema[] = []
  const seen = new Set<string>()
  for (const src of sources) {
    for (const c of nodeColumns(doc, schemas, previews, catalog, src)) {
      if (!seen.has(c.name)) { seen.add(c.name); out.push(c) }
    }
  }
  return out
}

// ---- schema warnings: a node's config references a column absent from its KNOWN input schema -------
// A non-enforcing, best-effort check: warn (never block) when a node points at a column that provably
// isn't in its input. Fires ONLY when the input schema is fully known AND the reference is reliably
// extractable — when unsure (untyped upstream, an expression we can't parse) it stays silent, so the
// cue reads as "you have a real problem", not noise.

// Bare words that appear in SQL-ish expressions but are NOT column references: keywords, operators,
// type names (CAST target), date parts (EXTRACT field), and no-paren special-value functions.
// Parenthesized functions are detected by a trailing '(' and qualified/lambda names are handled below.
const SQL_WORDS = new Set([
  'and', 'or', 'not', 'null', 'true', 'false', 'is', 'in', 'like', 'ilike', 'similar', 'between',
  'case', 'when', 'then', 'else', 'end', 'as', 'asc', 'desc', 'nulls', 'first', 'last', 'distinct',
  'on', 'using', 'cast', 'try_cast', 'exists', 'all', 'any', 'some', 'interval', 'from', 'where',
  'by', 'group', 'having', 'order', 'limit', 'offset', 'union', 'except', 'intersect', 'over',
  'partition', 'within', 'filter', 'collate', 'escape', 'default', 'symmetric', 'asymmetric',
  // operators/keywords built from bare words
  'to', 'at', 'zone', 'glob', 'regexp', 'notnull', 'isnull', 'unknown',
  // keyword-argument function words (substring/trim/overlay/... x FROM a FOR b)
  'for', 'both', 'leading', 'trailing', 'placing',
  // no-paren special-value functions
  'current_date', 'current_time', 'current_timestamp', 'localtime', 'localtimestamp',
  'current_user', 'session_user', 'current_schema', 'current_catalog',
  // type names (CAST / :: targets)
  'int', 'integer', 'bigint', 'smallint', 'tinyint', 'hugeint', 'float', 'double', 'real', 'decimal',
  'numeric', 'bool', 'boolean', 'varchar', 'char', 'text', 'string', 'blob', 'bytea', 'date',
  'timestamp', 'timestamptz', 'time', 'uuid', 'json', 'precision',
  // date parts (EXTRACT ... FROM / date_part)
  'year', 'month', 'day', 'hour', 'minute', 'second', 'millisecond', 'microsecond', 'week', 'quarter',
  'dow', 'doy', 'epoch', 'decade', 'century', 'millennium', 'era', 'isodow', 'isoyear', 'timezone',
])
const IDENT = /^(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))$/

/** A plain comma-separated identifier list (quoted or bare) → its names; null if any part is an
 * expression / `*` / function (so we never guess columns out of `lower(name) AS x`). */
function plainColumns(expr: string): string[] | null {
  if (!expr) return null
  const cols: string[] = []
  for (const part of expr.split(',')) {
    const m = part.trim().match(IDENT)
    if (!m) return null
    cols.push(m[1] ?? m[2])
  }
  return cols.length ? cols : null
}

/** The leading column of an ORDER-BY-ish item (`col DESC NULLS LAST` → `col`), or null if it's an expr. */
function leadingColumn(part: string): string | null {
  const t = part.trim().replace(/\s+(asc|desc)\b(\s+nulls\s+(first|last))?\s*$/i, '').trim()
  const m = t.match(IDENT)
  return m ? (m[1] ?? m[2]) : null
}

/** Best-effort column identifiers in a SQL-ish expression: skips string literals, function names
 * (ident before `(`), BOTH sides of a qualified/struct ref (`t.col`), lambda params (`x -> …`), and
 * SQL keywords/types/date-parts. Conservative — an over-skip is a silent miss, never a false warning. */
function exprColumns(expr: string): string[] {
  const s = expr.replace(/'(?:[^']|'')*'/g, ' ')  // drop single-quoted string literals
  // lambda bound vars (`x -> …`, DuckDB list/map lambdas) are not columns — exclude every occurrence
  const lambdaVars = new Set<string>()
  for (const lm of s.matchAll(/([A-Za-z_][A-Za-z0-9_]*)\s*->/g)) lambdaVars.add(lm[1].toLowerCase())
  const out: string[] = []
  const re = /"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(s))) {
    const quoted = m[1] != null
    const name = m[1] ?? m[2]
    if (/^\s*[.(]/.test(s.slice(re.lastIndex))) continue     // function call `ident(` OR qualifier `ident.`
    if (/\.\s*$/.test(s.slice(0, m.index))) continue         // qualified/struct field `.col`
    if (!quoted && (SQL_WORDS.has(name.toLowerCase()) || lambdaVars.has(name.toLowerCase()))) continue
    out.push(name)
  }
  return out
}

/** Columns a node's config references that we can extract RELIABLY (empty = don't know → don't warn). */
function referencedColumns(node: CanvasNode): string[] {
  const cfg = node.data.config as Record<string, unknown>
  const str = (k: string) => String(cfg[k] ?? '').trim()
  const plain = (v: string) => v.split(',').map(leadingColumn).filter((x): x is string => !!x)
  switch (node.type) {
    case 'select': return plainColumns(str('select') || str('expr')) ?? []
    case 'sort': return plain(str('by'))
    case 'dedup': return plain(str('on'))
    case 'aggregate': return plain(str('groupBy'))
    case 'filter': case 'assert': return exprColumns(str('predicate'))
    default: return []
  }
}

/** The lowercased column names available at a node's input, or null if the input schema is not fully
 * known (any upstream untyped/unresolved) — in which case a missing-column check would be a guess. */
function knownInputColumnSet(
  doc: CanvasDoc, schemas: SchemaMap, previews: PreviewMap, catalog: CatalogTable[], id: string,
): Set<string> | null {
  const srcs = doc.edges.filter((e) => e.target === id).map((e) => e.source)
  if (!srcs.length) return null
  const set = new Set<string>()
  for (const src of srcs) {
    if (schemas[src] === null) return null                 // untyped code op upstream → unknown
    const cols = nodeColumns(doc, schemas, previews, catalog, src)
    if (!cols.length) return null                          // unresolved / typed-but-empty → treat as unknown
    for (const c of cols) set.add(c.name.toLowerCase())
  }
  return set
}

/** Warnings for a node whose config references columns absent from its (known) input — [] when the
 * input is unknown or nothing reliably resolves as missing. Non-blocking: a soft cue, not an error. */
export function schemaWarnings(
  doc: CanvasDoc, schemas: SchemaMap, previews: PreviewMap, catalog: CatalogTable[], id: string,
): string[] {
  const node = doc.nodes.find((n) => n.id === id)
  if (!node) return []
  // a disabled node doesn't run and a bypassed node passes its input through, so its config isn't
  // applied — no warning applies (keeps the card/inspector/wire cues consistent with "won't run").
  if (node.data?.disabled || node.data?.bypassed) return []
  const refs = referencedColumns(node)
  if (!refs.length) return []
  const avail = knownInputColumnSet(doc, schemas, previews, catalog, id)
  if (!avail) return []
  const missing = [...new Set(refs.filter((r) => !avail.has(r.toLowerCase())))]
  return missing.length ? [`unknown column${missing.length > 1 ? 's' : ''}: ${missing.join(', ')}`] : []
}

/** A cheap stable hash of a string (djb2) — used to detect a transform cell changing after a schema
 * contract was pinned, so the contract can flag itself possibly-stale. */
export function codeHash(s: string): string {
  let h = 5381
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0
  return (h >>> 0).toString(36)
}
