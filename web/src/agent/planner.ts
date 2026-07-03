// The core ships a simple keyword planner (FR-A3). It only ever emits VALID graphs of
// generic node kinds; an org bundle can swap in a real LLM/code-mode planner behind the
// same shape. No domain knowledge here — it maps intent words to generic verbs.
import type { CatalogTable } from '../types/api'
import type { NodeConfig } from '../types/graph'

export interface PlanStep {
  kind: string
  title?: string
  config?: Partial<NodeConfig>
}

const RULES: { re: RegExp; step: (m: RegExpMatchArray) => PlanStep }[] = [
  { re: /\b(sample|subset|peek|preview|take \d+)\b/i, step: () => ({ kind: 'sample', config: { n: 1000, seed: 42 } }) },
  { re: /\bwhere\s+(.+?)(?:$|,|\.|then)/i, step: (m) => ({ kind: 'filter', config: { predicate: m[1].trim() } }) },
  { re: /\b(filter|clean|valid|remove|drop|exclude)\b/i, step: () => ({ kind: 'filter', config: { predicate: '' } }) },
  { re: /\b(dedup|dedupe|duplicate|near-dup)\b/i, step: () => ({ kind: 'transform', title: 'dedup', config: { source: 'adhoc', mode: 'map_batches', code: 'def fn(batch):\n    seen = set()\n    for r in batch:\n        key = str(sorted(r.items()))\n        r["dup"] = key in seen\n        seen.add(key)\n    return batch' } }) },
  { re: /\b(sql|query|select\b)\b/i, step: () => ({ kind: 'sql', config: { sql: 'SELECT * FROM input LIMIT 100' } }) },
  { re: /\b(join|combine|merge with)\b/i, step: () => ({ kind: 'join', config: { how: 'inner' } }) },
  { re: /\b(count|how many|average|mean|distribution|metric|total)\b/i, step: () => ({ kind: 'metric', config: { agg: 'count' } }) },
  { re: /\b(transform|add|compute|derive|map|new column|score|label|enrich|caption|embed)\b/i, step: () => ({ kind: 'transform', config: { source: 'adhoc', mode: 'map', code: 'def fn(row):\n    # agent stub — edit me\n    row["new_col"] = 1\n    return row' } }) },
  { re: /\b(write|save|materialize|output|export|table|training set|dataset)\b/i, step: () => ({ kind: 'write', config: { name: 'agent_output', writeMode: 'overwrite' } }) },
]

export function plan(intent: string, catalog: CatalogTable[], hasSource: boolean): { steps: PlanStep[]; summary: string } {
  // operators the intent actually asks for
  const ops: PlanStep[] = []
  const seen = new Set<string>()
  for (const rule of RULES) {
    const m = intent.match(rule.re)
    if (m) {
      const s = rule.step(m)
      const key = s.kind + (s.title ?? '')
      if (!seen.has(key)) { ops.push(s); seen.add(key) }
    }
  }

  const namedTable = catalog.find((t) => new RegExp(`\\b${escapeRe(t.name)}\\b`, 'i').test(intent))
  const mentionsData = /\b(load|read|use|open|dataset|table|data)\b/i.test(intent)

  // Nothing operational and no dataset referenced → we honestly can't turn this into a graph.
  if (ops.length === 0 && !namedTable && !mentionsData) {
    return { steps: [], summary: '' }
  }

  const steps: PlanStep[] = []
  if (!hasSource && (namedTable || ops.length > 0)) {
    const table = namedTable ?? catalog[0]
    if (table) steps.push({ kind: 'source', title: table.name, config: { uri: table.uri, tableId: table.id } })
  }
  steps.push(...ops)

  // add a terminal write only when the intent implies producing a dataset
  if (ops.length > 0 && /\b(training|table|dataset|output|clean|save|write|export)\b/i.test(intent) && !steps.some((s) => s.kind === 'write')) {
    steps.push({ kind: 'write', config: { name: 'agent_output', writeMode: 'overwrite' } })
  }

  return { steps, summary: steps.map((s) => s.title ?? s.kind).join(' → ') }
}

function escapeRe(s: string) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') }
