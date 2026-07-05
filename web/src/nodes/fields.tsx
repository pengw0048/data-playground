// Structured, schema-aware editing for expression fields (Phase 3). Columns flow along typed
// ports (see nodes/schema.ts), so filter/sort/etc. can offer real column pickers instead of blind
// text. Everything still round-trips to the same SQL string the kernel already understands, and a
// free-text fallback stays available for anything the builder can't express.
import { useState } from 'react'
import { useStore } from '../store/graph'
import { inputColumns } from './schema'
import { color, radius } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import type { ColumnSchema } from '../types/graph'

/** Columns available to a node's expression fields = its upstream output columns (typed ports).
 * Subscribes only to the things that affect columns (edges / schemas / previews / catalog) — NOT to
 * node positions, so dragging a node doesn't re-render every relational card. Node identity/config
 * is read non-reactively (a config change re-triggers a schema fetch, which updates `schemas`). */
export function useInputColumns(nodeId: string): ColumnSchema[] {
  const edges = useStore((s) => s.doc.edges)
  const schemas = useStore((s) => s.schemas)
  const previews = useStore((s) => s.previews)
  const catalog = useStore((s) => s.catalog)
  const nodes = useStore.getState().doc.nodes
  return inputColumns({ nodes, edges } as never, schemas, previews, catalog, nodeId)
}

const inputStyle = {
  fontSize: 11, color: color.ink, background: '#fff', border: `1px solid ${color.border}`,
  borderRadius: 6, padding: '5px 7px', width: '100%', outline: 'none',
} as const

let _dl = 0
/** A text input backed by a <datalist> of column names — autocomplete that still accepts free
 * expressions. When the upstream port is untyped (no columns known yet) it's just a text box. */
export function ColumnCombo({ value, columns, placeholder, onChange, mono = true }: {
  value: string; columns: ColumnSchema[]; placeholder?: string; onChange: (v: string) => void; mono?: boolean
}) {
  const [id] = useState(() => `dp-cols-${++_dl}`)
  return (
    <>
      <input
        list={columns.length ? id : undefined}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onClick={(e) => e.stopPropagation()}
        className={mono ? 'dp-mono' : undefined}
        style={inputStyle}
      />
      {columns.length > 0 && (
        <datalist id={id}>
          {columns.map((c) => <option key={c.name} value={c.name}>{c.type}</option>)}
        </datalist>
      )}
    </>
  )
}

// ---- sort: chips of {column, direction} ---------------------------------- //
interface SortKey { col: string; dir: 'ASC' | 'DESC' }

function parseSort(by: string): SortKey[] {
  return by.split(',').map((t) => t.trim()).filter(Boolean).map((t) => {
    const m = t.match(/^(.*?)(?:\s+(ASC|DESC))?$/i)
    return { col: (m?.[1] ?? t).trim(), dir: (m?.[2]?.toUpperCase() as 'ASC' | 'DESC') || 'ASC' }
  })
}
function serializeSort(keys: SortKey[]): string {
  return keys.filter((k) => k.col.trim()).map((k) => (k.dir === 'DESC' ? `${k.col} DESC` : k.col)).join(', ')
}

export function SortBuilder({ nodeId }: { nodeId: string }) {
  const by = String(useStore((s) => s.doc.nodes.find((n) => n.id === nodeId)?.data.config.by) ?? '')
  const updateConfig = useStore((s) => s.updateConfig)
  const columns = useInputColumns(nodeId)
  // a function expression can contain commas the chip-splitter would wrongly tear apart → free text
  const complex = by.includes('(')
  const [advanced, setAdvanced] = useState(complex)
  const keys = parseSort(by)
  const commit = (next: SortKey[]) => updateConfig(nodeId, { by: serializeSort(next) })

  if (advanced || complex) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        <ColumnCombo value={by} columns={columns} placeholder="score DESC, id"
          onChange={(v) => updateConfig(nodeId, { by: v })} />
        <button className="nodrag" onClick={(e) => { e.stopPropagation(); if (!by.includes('(')) setAdvanced(false) }}
          style={{ ...addBtn, opacity: by.includes('(') ? 0.5 : 1 }} title={by.includes('(') ? 'Contains an expression — edit as text' : 'Switch to the builder'}>
          <Icon name="fx" size={11} /> builder
        </button>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      {keys.map((k, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <div style={{ flex: 1 }}>
            <ColumnCombo value={k.col} columns={columns} placeholder="column"
              onChange={(v) => commit(keys.map((x, j) => (j === i ? { ...x, col: v } : x)))} />
          </div>
          <button className="nodrag" onClick={(e) => { e.stopPropagation(); commit(keys.map((x, j) => (j === i ? { ...x, dir: x.dir === 'ASC' ? 'DESC' : 'ASC' } : x))) }}
            title="Toggle direction" style={dirBtn}>{k.dir}</button>
          <button className="nodrag" onClick={(e) => { e.stopPropagation(); commit(keys.filter((_, j) => j !== i)) }}
            title="Remove" style={xBtn}><Icon name="close" size={11} /></button>
        </div>
      ))}
      <div style={{ display: 'flex', gap: 6 }}>
        <button className="nodrag" onClick={(e) => { e.stopPropagation(); commit([...keys, { col: columns[0]?.name ?? '', dir: 'ASC' }]) }}
          style={addBtn}><Icon name="plus" size={11} /> add sort key</button>
        <button className="nodrag" onClick={(e) => { e.stopPropagation(); setAdvanced(true) }}
          style={addBtn} title="Edit order-by as text">raw</button>
      </div>
    </div>
  )
}

// ---- filter: rows of {column, op, value} joined by AND ------------------- //
const OPS = ['=', '!=', '>', '>=', '<', '<=', 'LIKE', 'IS NULL', 'IS NOT NULL'] as const
type Op = typeof OPS[number]
interface Cond { col: string; op: Op; val: string }

// Parse a predicate into simple conditions; returns null if it isn't a plain "a op b AND c op d"
// (compound OR, parentheses, functions) — then we stay in free-text ("advanced") mode.
function parseFilter(pred: string): Cond[] | null {
  const p = pred.trim()
  if (!p) return []
  if (/\bor\b|\(|\)/i.test(p)) return null
  const parts = p.split(/\s+AND\s+/i)
  const conds: Cond[] = []
  for (const part of parts) {
    const nul = part.match(/^(.+?)\s+(IS NOT NULL|IS NULL)$/i)
    if (nul) { conds.push({ col: nul[1].trim(), op: nul[2].toUpperCase() as Op, val: '' }); continue }
    const m = part.match(/^(.+?)\s*(!=|>=|<=|=|>|<|LIKE)\s*(.+)$/i)
    if (!m) return null
    conds.push({ col: m[1].trim(), op: m[2].toUpperCase() as Op, val: m[3].trim() })
  }
  return conds
}
function literal(val: string, colType: string | undefined): string {
  // A filter VALUE is treated as a literal. Numbers / bool / null / already-quoted pass through;
  // everything else is quoted as a string (so a value that happens to equal a column name isn't
  // silently turned into a column reference). Column-vs-column comparisons use the raw-SQL toggle.
  const v = val.trim()
  if (v === '') return "''"
  if (/^-?\d+(\.\d+)?$/.test(v) || /^(true|false|null)$/i.test(v) || /^'.*'$/.test(v)) return v
  // date/time/timestamp values need quoting too (unquoted 2024-01-01 = integer arithmetic)
  const stringy = !colType || /string|json|struct|list|bytes|date|time|timestamp/.test(colType)
  return stringy ? `'${v.replace(/'/g, "''")}'` : v
}
function serializeFilter(conds: Cond[], columns: ColumnSchema[]): string {
  return conds.filter((c) => c.col.trim()).map((c) => {
    if (c.op === 'IS NULL' || c.op === 'IS NOT NULL') return `${c.col} ${c.op}`
    const t = columns.find((x) => x.name === c.col)?.type
    return `${c.col} ${c.op} ${literal(c.val, t)}`
  }).join(' AND ')
}

export function FilterBuilder({ nodeId }: { nodeId: string }) {
  const pred = String(useStore((s) => s.doc.nodes.find((n) => n.id === nodeId)?.data.config.predicate) ?? '')
  const updateConfig = useStore((s) => s.updateConfig)
  const columns = useInputColumns(nodeId)
  const parsed = parseFilter(pred)
  // stick in advanced mode if the current predicate can't be represented as simple AND conditions
  const [advanced, setAdvanced] = useState(parsed === null)
  const conds = parsed ?? []
  const commit = (next: Cond[]) => updateConfig(nodeId, { predicate: serializeFilter(next, columns) })

  if (advanced || parsed === null) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        <ColumnCombo value={pred} columns={columns} placeholder="is_valid = true AND score > 0.5"
          onChange={(v) => updateConfig(nodeId, { predicate: v })} />
        <button className="nodrag" onClick={(e) => { e.stopPropagation(); if (parseFilter(pred) !== null) setAdvanced(false) }}
          style={{ ...addBtn, opacity: parseFilter(pred) === null ? 0.5 : 1 }} title={parseFilter(pred) === null ? 'Simplify the predicate to use the builder' : 'Switch to the builder'}>
          <Icon name="fx" size={11} /> builder
        </button>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      {conds.map((c, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <ColumnCombo value={c.col} columns={columns} placeholder="column"
              onChange={(v) => commit(conds.map((x, j) => (j === i ? { ...x, col: v } : x)))} />
          </div>
          <select className="nodrag" value={c.op} onClick={(e) => e.stopPropagation()}
            onChange={(e) => commit(conds.map((x, j) => (j === i ? { ...x, op: e.target.value as Op } : x)))}
            style={{ ...inputStyle, width: 'auto', cursor: 'pointer', flex: '0 0 auto' }}>
            {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          {c.op !== 'IS NULL' && c.op !== 'IS NOT NULL' && (
            <div style={{ flex: 1, minWidth: 0 }}>
              {/* a literal value — no column suggestions (that would invite the value→column confusion) */}
              <ColumnCombo value={c.val} columns={[]} placeholder="value"
                onChange={(v) => commit(conds.map((x, j) => (j === i ? { ...x, val: v } : x)))} />
            </div>
          )}
          <button className="nodrag" onClick={(e) => { e.stopPropagation(); commit(conds.filter((_, j) => j !== i)) }}
            title="Remove" style={xBtn}><Icon name="close" size={11} /></button>
        </div>
      ))}
      <div style={{ display: 'flex', gap: 6 }}>
        <button className="nodrag" onClick={(e) => { e.stopPropagation(); commit([...conds, { col: columns[0]?.name ?? '', op: '=', val: '' }]) }}
          style={addBtn}><Icon name="plus" size={11} /> add condition</button>
        <button className="nodrag" onClick={(e) => { e.stopPropagation(); setAdvanced(true) }}
          style={addBtn} title="Edit the raw SQL predicate">raw SQL</button>
      </div>
    </div>
  )
}

const dirBtn = {
  border: `1px solid ${color.border}`, background: '#fff', color: color.text2, fontSize: 9.5, fontWeight: 700,
  letterSpacing: 0.4, padding: '4px 6px', borderRadius: 6, cursor: 'pointer', flex: '0 0 auto',
} as const
const xBtn = {
  border: 'none', background: 'transparent', color: color.text3, cursor: 'pointer', display: 'grid',
  placeItems: 'center', width: 20, height: 20, flex: '0 0 auto',
} as const
const addBtn = {
  display: 'inline-flex', alignItems: 'center', gap: 4, border: `1px dashed ${color.border}`, background: 'transparent',
  color: color.text3, fontSize: 10.5, padding: '4px 8px', borderRadius: radius.chip, cursor: 'pointer',
} as const
