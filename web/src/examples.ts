// One-click example canvases. Each is a runnable starter built ONLY from built-in nodes on the SEEDED
// datasets (events/movies/images resolve by bare name via the catalog), so it runs immediately on a
// fresh install — the fastest way to see a real pipeline before building your own.
import type { CanvasDoc, CanvasEdge, CanvasNode } from './types/graph'

interface TmplNode { id: string; type: string; title?: string; config: Record<string, unknown> }
interface Tmpl { key: string; name: string; blurb: string; nodes: TmplNode[]; chain: string[] }

const TEMPLATES: Tmpl[] = [
  {
    key: 'purchases',
    name: 'Purchases per user',
    blurb: 'events → keep purchases → total per user → sort → save. The clean → summarize → export basics.',
    nodes: [
      { id: 'src', type: 'source', title: 'events', config: { uri: 'events' } },
      { id: 'flt', type: 'filter', config: { predicate: "event = 'purchase'" } },
      { id: 'agg', type: 'aggregate', config: { groupBy: 'user_id', aggs: 'sum(amount) AS total, count(*) AS n' } },
      { id: 'srt', type: 'sort', config: { by: 'total DESC' } },
      { id: 'out', type: 'write', title: 'top_users', config: { name: 'top_users' } },
    ],
    chain: ['src', 'flt', 'agg', 'srt', 'out'],
  },
  {
    key: 'top3',
    name: 'Top 3 events per user',
    blurb: 'A window function ranks each user’s events by amount and keeps the top 3. Shows the window node.',
    nodes: [
      { id: 'src', type: 'source', title: 'events', config: { uri: 'events' } },
      { id: 'win', type: 'window', config: { expr: 'row_number()', partitionBy: 'user_id', orderBy: 'amount DESC', as: 'rank' } },
      { id: 'flt', type: 'filter', config: { predicate: 'rank <= 3' } },
      { id: 'out', type: 'write', title: 'top3_per_user', config: { name: 'top3_per_user' } },
    ],
    chain: ['src', 'win', 'flt', 'out'],
  },
  {
    key: 'quality',
    name: 'Data-quality check',
    blurb: 'An assert gate flags events whose amount is over 100 — preview the node to see the violating rows.',
    nodes: [
      { id: 'src', type: 'source', title: 'events', config: { uri: 'events' } },
      { id: 'chk', type: 'assert', title: 'amount ≤ 100?', config: { predicate: 'amount <= 100', severity: 'warn' } },
    ],
    chain: ['src', 'chk'],
  },
]

function toDoc(t: Tmpl, id: string): CanvasDoc {
  const order = new Map(t.chain.map((nid, i) => [nid, i]))
  const nodes: CanvasNode[] = t.nodes.map((n) => ({
    id: n.id,
    type: n.type,
    position: { x: 80 + (order.get(n.id) ?? 0) * 280, y: 180 },
    data: { title: n.title ?? n.type, status: 'draft', config: n.config },
  }))
  const edges: CanvasEdge[] = t.chain.slice(1).map((tgt, i) => {
    const src = t.chain[i]
    return { id: `e_${src}_${tgt}`, source: src, target: tgt, sourceHandle: null, targetHandle: null, data: { wire: 'dataset' } }
  })
  return { id, name: t.name, version: 1, nodes, edges }
}

export interface ExampleMeta { key: string; name: string; blurb: string }
export const examples: ExampleMeta[] = TEMPLATES.map((t) => ({ key: t.key, name: t.name, blurb: t.blurb }))
export function exampleDoc(key: string, id: string): CanvasDoc | null {
  const t = TEMPLATES.find((x) => x.key === key)
  return t ? toDoc(t, id) : null
}
