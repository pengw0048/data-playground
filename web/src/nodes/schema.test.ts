import { describe, it, expect } from 'vitest'
import { schemaWarnings, codeHash } from './schema'

// minimal doc/schema builders — schemaWarnings only reads node.type/config, edges, and the schema map.
const node = (id: string, type: string, config: Record<string, unknown> = {}) =>
  ({ id, type, position: { x: 0, y: 0 }, data: { title: id, config, status: 'draft' } })
const edge = (s: string, t: string, th?: string) =>
  ({ id: `${s}-${t}`, source: s, target: t, sourceHandle: null, targetHandle: th ?? null, data: { wire: 'dataset' } })
const cols = (...names: string[]) => names.map((n) => ({ name: n, type: 'x', capabilities: [] as string[] }))

// one source `s` feeding a single consumer `f`; `schemas.s` is the source's (known) output.
function warn(consumer: ReturnType<typeof node>, sCols: unknown) {
  const doc = { id: 'c', version: 1, nodes: [node('s', 'source'), consumer], edges: [edge('s', consumer.id)] } as never
  return schemaWarnings(doc, { s: sCols } as never, {} as never, [] as never, consumer.id)
}

describe('schemaWarnings — column references vs known input', () => {
  it('is silent when the referenced column exists', () => {
    expect(warn(node('f', 'filter', { predicate: 'score > 0' }), cols('score', 'id'))).toEqual([])
  })

  it('warns when a filter references a missing column', () => {
    const w = warn(node('f', 'filter', { predicate: 'score > 0' }), cols('id', 'user_id'))
    expect(w).toHaveLength(1)
    expect(w[0]).toContain('score')
  })

  it('stays silent when the upstream is untyped (null) — cannot check', () => {
    expect(warn(node('f', 'filter', { predicate: 'score > 0' }), null)).toEqual([])
  })

  it('stays silent when the upstream schema is unknown (undefined)', () => {
    const doc = { id: 'c', version: 1, nodes: [node('s', 'source'), node('f', 'filter', { predicate: 'nope > 0' })], edges: [edge('s', 'f')] } as never
    expect(schemaWarnings(doc, {} as never, {} as never, [] as never, 'f')).toEqual([])
  })

  it('checks a plain select column list but skips expressions', () => {
    expect(warn(node('x', 'select', { select: 'id, missing' }), cols('id', 'name'))).toHaveLength(1)
    expect(warn(node('x', 'select', { select: 'id, name' }), cols('id', 'name'))).toEqual([])
    // an expression list is not a plain column list → we don't guess columns out of it
    expect(warn(node('x', 'select', { select: 'lower(name) AS n' }), cols('id', 'name'))).toEqual([])
  })

  it('checks sort / dedup / groupBy leading columns, skipping expressions & sort direction', () => {
    expect(warn(node('x', 'sort', { by: 'score DESC' }), cols('id'))).toHaveLength(1)          // score missing
    expect(warn(node('x', 'sort', { by: 'id DESC NULLS LAST' }), cols('id'))).toEqual([])       // exists
    expect(warn(node('x', 'dedup', { on: 'id, gone' }), cols('id'))).toHaveLength(1)            // gone missing
    expect(warn(node('x', 'aggregate', { groupBy: 'date_trunc(\'day\', ts)' }), cols('ts'))).toEqual([]) // expr → skip
  })

  it('does NOT false-positive on functions, type names, date parts, or string literals', () => {
    expect(warn(node('f', 'filter', { predicate: "date_trunc('day', ts) > now()" }), cols('ts'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: 'CAST(amount AS INTEGER) > 1' }), cols('amount'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: "status = 'active' AND n IS NOT NULL" }), cols('status', 'n'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: "extract(year from ts) = 2024" }), cols('ts'))).toEqual([])
  })

  it('is case-insensitive when matching column names', () => {
    expect(warn(node('f', 'filter', { predicate: 'Score > 0' }), cols('score'))).toEqual([])
  })

  it('does not check kinds we cannot parse (sql, transform)', () => {
    expect(warn(node('q', 'sql', { sql: 'SELECT nope FROM input' }), cols('id'))).toEqual([])
    expect(warn(node('t', 'transform', { code: 'def fn(r): return r' }), cols('id'))).toEqual([])
  })

  // ---- adversarial-review false-positive regressions (all must stay silent on valid predicates) ----
  it('does not flag no-paren special-value functions', () => {
    expect(warn(node('f', 'filter', { predicate: 'created_at >= current_date' }), cols('created_at'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: 'ts > current_timestamp' }), cols('ts'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: 'ts < localtimestamp' }), cols('ts'))).toEqual([])
  })

  it('does not flag keyword-argument function words (substring/trim FROM/FOR/BOTH)', () => {
    expect(warn(node('f', 'filter', { predicate: "substring(name FROM 1 FOR 2) = 'ab'" }), cols('name'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: "trim(BOTH ' ' FROM label) = 'x'" }), cols('label'))).toEqual([])
  })

  it('does not flag bare-word operators (SIMILAR TO / AT TIME ZONE / GLOB / NOTNULL)', () => {
    expect(warn(node('f', 'filter', { predicate: "name SIMILAR TO 'a%'" }), cols('name'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: "ts AT TIME ZONE 'UTC' > x" }), cols('ts', 'x'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: "path GLOB '*.txt'" }), cols('path'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: 'email NOTNULL' }), cols('email'))).toEqual([])
  })

  it('does not flag a table/relation qualifier before a dot', () => {
    expect(warn(node('f', 'filter', { predicate: 'orders.amount > 0' }), cols('amount'))).toEqual([])
    expect(warn(node('f', 'filter', { predicate: 't1.x = t2.y' }), cols('x', 'y'))).toEqual([])
  })

  it('does not flag lambda bound variables', () => {
    expect(warn(node('f', 'filter', { predicate: 'len(list_filter(nums, y -> y > 0)) > 0' }), cols('nums'))).toEqual([])
  })

  it('treats a typed-but-empty upstream as unknown (silent), not "everything missing"', () => {
    expect(warn(node('f', 'filter', { predicate: 'score > 0' }), [])).toEqual([])
  })

  it('stays silent on a disabled or bypassed node (its config is not applied)', () => {
    const dis = { ...node('f', 'filter', { predicate: 'gone > 0' }), data: { title: 'f', config: { predicate: 'gone > 0' }, status: 'draft', disabled: true } }
    const byp = { ...node('f', 'filter', { predicate: 'gone > 0' }), data: { title: 'f', config: { predicate: 'gone > 0' }, status: 'draft', bypassed: true } }
    expect(warn(dis as never, cols('id'))).toEqual([])
    expect(warn(byp as never, cols('id'))).toEqual([])
  })

  it('still catches a genuinely missing column after all the guards', () => {
    expect(warn(node('f', 'filter', { predicate: 'missing_col > 0 AND id = 1' }), cols('id'))[0]).toContain('missing_col')
  })
})

describe('codeHash', () => {
  it('is stable and distinguishes different cells', () => {
    expect(codeHash('def fn(r): return r')).toBe(codeHash('def fn(r): return r'))
    expect(codeHash('def fn(r): return r')).not.toBe(codeHash('def fn(r): return {**r, "x": 1}'))
  })
})
