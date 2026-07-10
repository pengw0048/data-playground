import { useEffect, useState } from 'react'
import { useStore, nodeRunnable } from '../store/graph'
import { getSpec } from '../nodes/registry'
import { getBackendSpec, NodeParamFields, nodeInvalidReason } from '../nodes/generic'
import { useSchemaWarnings } from '../nodes/fields'
import { codeHash } from '../nodes/schema'
import { color, status as statusTok, kindAccent } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { FileDialog } from '../ui/FileDialog'
import { miniInputClass } from '../ui/controls'
import { api } from '../api/client'
import type { JoinAnalysis, JoinSuggestion } from '../types/api'
import type { ColumnSchema } from '../types/graph'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Separator } from '@/components/ui/separator'
import { cn } from '@/lib/utils'

export const INSPECTOR_W = 300

// Built-in node kinds with a hand-built card — everything else the app renders is a PLUGIN node.
const BUILTIN_KINDS = new Set([
  'source', 'filter', 'select', 'sort', 'dedup', 'join', 'sql', 'aggregate', 'sample',
  'metric', 'chart', 'write', 'note', 'section', 'code', 'transform', 'vector-search',
])
// Kinds whose OUTPUT columns need running code, so they can carry a user schema contract — mirrors the
// kernel's _UNTYPED set (minus 'section', which has its own port editor). Plugin kinds execute too, so
// any non-built-in kind is contract-capable as well. Relational/io/annotation nodes are always typed.
const CONTRACT_KINDS = new Set(['transform', 'notebook', 'vector-search', 'loop', 'opaque'])
export const canDeclareSchemaKind = (kind: string) => CONTRACT_KINDS.has(kind) || !BUILTIN_KINDS.has(kind)

// Figma-style right property panel: shows the SELECTED node's properties (params reused from the
// generic editor), a code snippet with "open editor", its ports, and actions. When nothing (or a
// multi-selection) is selected it shows a hint. The canvas cards still work; this is the persistent
// place to inspect/edit one node.
export function Inspector() {
  const selectedIds = useStore((s) => s.selectedIds)
  const nodes = useStore((s) => s.doc.nodes)
  const id = selectedIds.length === 1 ? selectedIds[0] : null
  const node = id ? nodes.find((n) => n.id === id) : null

  return (
    <aside data-testid="inspector"
      className="flex h-full flex-col overflow-hidden border-l border-border bg-card"
      style={{ width: INSPECTOR_W, flex: `0 0 ${INSPECTOR_W}px` }}>
      <div className="flex h-[52px] flex-none items-center border-b border-border px-3.5 text-[13px] font-semibold text-foreground">
        Inspector
      </div>
      {node ? <NodeInspector key={node.id} nodeId={node.id} />
        : <Empty text={selectedIds.length > 1 ? `${selectedIds.length} nodes selected` : 'Select a node to see its properties'} />}
    </aside>
  )
}

function Empty({ text }: { text: string }) {
  return (
    <div className="grid flex-1 place-items-center p-6 text-center text-xs leading-relaxed text-muted-foreground">
      {text}
    </div>
  )
}

function NodeInspector({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const runnable = useStore((s) => nodeRunnable(s.doc, nodeId))
  const runState = useStore((s) => s.runs[nodeId]?.phase)
  const serverSchema = useStore((s) => s.schemas[nodeId])  // ColumnSchema[] = typed · null = untyped · undefined = unknown
  const allSchemas = useStore((s) => s.schemas)
  const edges = useStore((s) => s.doc.edges)
  const warnings = useSchemaWarnings(nodeId)   // config references a column not in the (known) input
  const { rename, runPreview, requestRun, cancelRun, togglePanel, bypass, disable, duplicate, removeNode, openCodeFullscreen } = useStore.getState()
  const [name, setName] = useState(node?.data.title ?? '')
  useEffect(() => setName(node?.data.title ?? ''), [node?.data.title])
  if (!node) return null

  const kind = node.type
  const spec = getSpec(kind)
  const bspec = getBackendSpec(kind)
  const st = statusTok[node.data.status] ?? statusTok.draft
  const codeParams = (bspec?.params ?? []).filter((p) => p.type === 'code')
  const cfg = node.data.config as Record<string, unknown>
  const invalid = nodeInvalidReason(node)

  // a code op / plugin kind can carry a declared/inferred schema contract; relational ops are always
  // statically typed, and source/write/note/section are handled elsewhere.
  const canDeclareSchema = canDeclareSchemaKind(kind) && !['source', 'write', 'note', 'section'].includes(kind)
  // OUTPUT port schema: prefer the node's own declared contract (exact user types, instant) over the
  // server-resolved schema — but only for a contract-capable, non-bypassed node (a bypassed node passes
  // its input through, so its declaration doesn't describe its output). null = untyped, undefined = unknown.
  const declaredOut = Array.isArray(cfg.outputSchema) && (cfg.outputSchema as ColumnSchema[]).length
    ? (cfg.outputSchema as ColumnSchema[]) : null
  const outSchema = (canDeclareSchema && declaredOut && !node.data.bypassed) ? declaredOut : serverSchema
  // INPUT port schema = the OUTPUT schema of whatever is wired into that port (routed by targetHandle).
  const inputSchemaFor = (portId: string): ColumnSchema[] | null | undefined => {
    const inc = edges.filter((e) => e.target === nodeId)
    const specIn = spec?.inputs ?? []
    const e = inc.find((ed) => (ed.targetHandle ?? specIn[0]?.id ?? 'in') === portId)
      ?? (specIn.length === 1 ? inc[0] : undefined)
    return e ? allSchemas[e.source] : undefined
  }

  return (
    <div className="flex flex-1 flex-col overflow-y-auto">
      {/* header */}
      <div className="flex flex-col gap-2 border-b border-border px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className="h-[26px] w-1 flex-none rounded-sm" style={{ background: kindAccent[kind] ?? color.text3 }} />
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => { if (name.trim() && name !== node.data.title) rename(nodeId, name.trim()) }}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
            className="min-w-0 flex-1 rounded-md border border-transparent bg-transparent px-1.5 py-[3px] text-sm font-semibold text-foreground outline-none transition-colors focus:border-border"
          />
          <span className="rounded bg-muted px-1.5 py-0.5 text-[8.5px] font-semibold tracking-[0.6px] text-muted-foreground">
            {(spec?.tag ?? kind).toUpperCase()}
          </span>
        </div>
        <div className="flex items-center gap-1.5 text-[11.5px] text-muted-foreground">
          {/* a note is an annotation — it never runs, so a run status (draft/stale/…) is meaningless */}
          {kind === 'note' ? <span>annotation</span>
            : <><span style={{ color: st.color }}>{st.glyph}</span> {st.label}</>}
          {spec?.blurb && <span className="text-muted-foreground/70">· {spec.blurb}</span>}
        </div>
      </div>

      {/* properties (reused generic param editor) */}
      <Section title="Properties">
        <NodeParamFields nodeId={nodeId} />
        {codeParams.length === 0 && (bspec?.params ?? []).length === 0 && kind !== 'write' && (
          <div className="text-[11.5px] text-muted-foreground">No editable parameters.</div>
        )}
      </Section>

      {/* a write node's output destination lives here in the panel, not cluttering the card */}
      {kind === 'write' && <WriteDestination nodeId={nodeId} />}

      {/* code snippet + open the full editor (Monaco panel; fullscreen editor is a later step) */}
      {codeParams.map((p) => {
        const codeText = String(cfg[p.name] ?? p.default ?? '')
        return (
          <Section key={p.name} title={p.label ?? p.name}>
            <pre className="dp-mono m-0 max-h-[120px] overflow-auto whitespace-pre rounded-lg border border-border p-2 text-[10.5px] leading-normal text-muted-foreground"
              style={{ background: 'var(--code-bg, #f7f8fa)' }}>
              {codeText || '(empty)'}
            </pre>
            <div className="mt-1.5 flex gap-1.5">
              {kind === 'section' ? (
                <CodeBtn icon="code" label="Open section editor →" onClick={() => togglePanel(nodeId, 'section')} />
              ) : (
                <CodeBtn icon="external" label="Open fullscreen editor" onClick={() => openCodeFullscreen(nodeId, p.name, p.lang)} />
              )}
            </div>
          </Section>
        )
      })}

      {/* catalog-driven join hints: suggested keys (measured cardinality) + a fan-out warning */}
      {kind === 'join' && <JoinHints nodeId={nodeId} />}

      {/* compute placement: what this step needs → the run routes to a matching worker (e.g. a GPU pool) */}
      {(kind === 'transform' || kind === 'section') && <ResourcesSection nodeId={nodeId} />}

      {/* checkpoint: materialize this node's output → inspectable + reused across runs (splits a region) */}
      {kind !== 'source' && kind !== 'note' && kind !== 'write' && <CheckpointToggle nodeId={nodeId} />}

      {/* run plan: appears only when placement actually splits/routes this run (a cluster backend, an
          engine label, or a checkpoint) — makes the cost-aware scheduler + tiering visible before running */}
      {kind !== 'note' && <RunPlan nodeId={nodeId} />}

      {/* ports — a real port label (join left/right, metric value) shows as a name; the default
          in/out ports show their wire type + a typed/untyped schema badge (click "N cols" to expand
          the columns). Input badges reflect the upstream's output schema. */}
      <Section title="Ports">
        <div className="flex flex-col gap-1 text-[11.5px] text-muted-foreground">
          {(spec?.inputs ?? []).map((p) => <PortRow key={`in-${p.id}`} dir="in" name={portName(p)} wire={p.wire} schema={inputSchemaFor(p.id)} />)}
          {(spec?.outputs ?? []).map((p, i) => (
            <PortRow key={`out-${p.id}`} dir="out" name={portName(p)} wire={p.wire}
              schema={i === 0 ? (outSchema === undefined ? undefined : outSchema) : undefined} />
          ))}
          {(spec?.inputs ?? []).length === 0 && (spec?.outputs ?? []).length === 0 && <span>—</span>}
        </div>
        {/* editable output ports: only on the section (its driver script emit()s to named ports) —
            fixed-port ops (filter/sort/join) keep their ports as a type contract the wires rely on */}
        {kind === 'section' && <><Separator className="my-1" /><OutputPortsEditor nodeId={nodeId} /></>}
      </Section>

      {/* schema contract: a code op (transform/plugin/vector-search) is untyped until it runs — let the
          user DECLARE its output columns, or infer them from a sample. Either way types it + downstream. */}
      {canDeclareSchema && <SchemaContract nodeId={nodeId} runnable={runnable && !invalid} />}

      {/* actions */}
      <Section title="Actions">
        {invalid && <div className="mb-1.5 text-[11px] text-amber-700">⚠ {invalid}</div>}
        {!invalid && warnings.map((w, i) => (
          <div key={i} className="mb-1.5 text-[11px] text-amber-700 dark:text-amber-300">⚠ {w} — not found in the input schema</div>
        ))}
        <div className="flex flex-wrap gap-1.5">
          {/* a note never runs — only offer duplicate / delete for annotations */}
          {kind !== 'note' && <>
            <Action icon="eye" label="View data" disabled={!runnable || !!invalid} onClick={() => runPreview(nodeId)} />
            <Action icon={runState === 'running' ? 'stop' : 'play'} label={kind === 'source' ? 'Count rows' : runState === 'running' ? 'Stop' : 'Run'} disabled={(!runnable || !!invalid) && runState !== 'running'}
              onClick={() => (runState === 'running' ? cancelRun(nodeId) : requestRun(nodeId))} />
            {spec?.canBypass && <Action icon="power" label="Bypass" onClick={() => bypass(nodeId)} />}
            <Action icon="mute" label={node.data.disabled ? 'Enable' : 'Disable'} onClick={() => disable(nodeId)} />
          </>}
          <Action icon="duplicate" label="Duplicate" onClick={() => duplicate(nodeId)} />
          <Action icon="trash" label="Delete" danger onClick={() => removeNode(nodeId)} />
        </div>
      </Section>
    </div>
  )
}

// Add / rename / remove a section's named output ports (config.outputs). The store drops edges
// leaving a port that no longer exists, so a rename/remove can't strand an invisible wire.
function OutputPortsEditor({ nodeId }: { nodeId: string }) {
  // select the stored value (stable ref) — NOT a freshly-built array, which would loop forever
  const raw = useStore((s) => (s.doc.nodes.find((n) => n.id === nodeId)?.data.config as { outputs?: unknown } | undefined)?.outputs)
  const outputs = Array.isArray(raw) && raw.length ? raw.map(String) : ['out']
  const updateConfig = useStore((s) => s.updateConfig)
  const commit = (next: string[]) => updateConfig(nodeId, { outputs: next })
  return (
    <div className="mt-2 flex flex-col gap-1">
      <Label className="text-[9.5px] font-bold uppercase tracking-[0.4px] text-muted-foreground">OUTPUT PORTS (emit)</Label>
      {outputs.map((name, i) => (
        <div key={i} className="flex items-center gap-1">
          <Input value={name} onChange={(e) => commit(outputs.map((x, j) => (j === i ? e.target.value.replace(/\s+/g, '_') : x)))}
            className={cn(miniInputClass, 'dp-mono flex-1 text-[11px] md:text-[11px]')} />
          {outputs.length > 1 && (
            <Button variant="ghost" size="icon" onClick={() => commit(outputs.filter((_, j) => j !== i))} title="Remove port"
              className="h-5 w-5 flex-none text-muted-foreground [&_svg]:size-3"><Icon name="close" size={11} /></Button>
          )}
        </div>
      ))}
      <Button variant="outline" size="sm" onClick={() => commit([...outputs, `out${outputs.length + 1}`])}
        className="h-auto gap-1 self-start border-dashed px-2 py-1 text-[10.5px] font-medium text-muted-foreground shadow-none [&_svg]:size-3">
        <Icon name="plus" size={11} /> add port
      </Button>
    </div>
  )
}

// The write node's output destination — chosen here in the property panel via the save dialog.
function WriteDestination({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const [dlg, setDlg] = useState(false)
  const cfg = (node?.data.config ?? {}) as Record<string, unknown>
  const filename = String(cfg.filename ?? cfg.name ?? 'output.parquet')
  const destName = (cfg.destName as string) ?? 'Workspace outputs'
  const destPath = String(cfg.destPath ?? '')
  return (
    <Section title="Output">
      <div className="text-[11.5px] text-muted-foreground">
        <div className="dp-mono text-foreground">{filename}</div>
        <div className="mt-[3px] flex items-center gap-[5px] text-muted-foreground">
          <Icon name="export" size={11} /> {destName}{destPath ? `/${destPath}` : ''}
        </div>
      </div>
      <div className="mt-2">
        <CodeBtn icon="export" label="Change destination…" onClick={() => setDlg(true)} />
      </div>
      {dlg && (
        <FileDialog mode="save" defaultName={filename} onClose={() => setDlg(false)}
          onPick={(r) => { updateConfig(nodeId, { destId: r.destId, destName: r.destName, destPath: r.path, filename: r.filename }); setDlg(false) }} />
      )}
    </Section>
  )
}

function CodeBtn({ icon, label, onClick }: { icon: IconName; label: string; onClick: () => void }) {
  return (
    <Button variant="outline" size="sm" onClick={onClick}
      className="h-auto gap-1.5 px-2.5 py-1.5 text-[11.5px] font-medium text-primary shadow-none [&_svg]:size-3">
      <Icon name={icon} size={12} /> {label}
    </Button>
  )
}

function CheckpointToggle({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const on = !!(node?.data.config as Record<string, unknown>)?.checkpoint
  return (
    <Section title="Materialization">
      <button data-testid="checkpoint-toggle" onClick={() => updateConfig(nodeId, { checkpoint: on ? undefined : true })}
        className="flex w-full items-start gap-2 rounded-md border border-border px-2.5 py-2 text-left hover:bg-accent">
        <span className={cn('mt-[3px] h-2.5 w-2.5 shrink-0 rounded-full', on ? 'bg-primary' : 'border border-muted-foreground')} />
        <span className="min-w-0 flex-1">
          <span className="block text-[11.5px] font-medium text-foreground">{on ? 'Checkpointed' : 'Checkpoint here'}</span>
          <span className="mt-0.5 block text-[10.5px] leading-snug text-muted-foreground">{on ? 'Output materialized — inspectable and reused across runs.' : 'Materialize this step’s output.'}</span>
        </span>
      </button>
    </Section>
  )
}

function ResourcesSection({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const req = ((node?.data.config as Record<string, unknown>)?.requires ?? {}) as { cpu?: number; gpu?: number; gpuType?: string }
  const set = (patch: Record<string, unknown>) => {
    const next: Record<string, unknown> = { ...req, ...patch }
    for (const k of Object.keys(next)) if (next[k] === '' || next[k] == null) delete next[k]
    updateConfig(nodeId, { requires: Object.keys(next).length ? next : undefined })
  }
  const num = (v: string) => (v === '' ? undefined : Number(v))
  return (
    <Section title="Resources (placement)">
      <div className="mb-1.5 text-[10.5px] leading-relaxed text-muted-foreground">
        What this step needs — the run routes to a worker that satisfies it (e.g. a GPU pool). Blank = no requirement.
      </div>
      <div className="grid grid-cols-3 gap-1.5">
        <label className="flex flex-col gap-0.5 text-[10px] text-muted-foreground">GPUs
          <Input type="number" min={0} className="h-7 text-[11.5px] md:text-[11.5px]" value={req.gpu ?? ''} onChange={(e) => set({ gpu: num(e.target.value) })} /></label>
        <label className="flex flex-col gap-0.5 text-[10px] text-muted-foreground">GPU type
          <Input className="h-7 text-[11.5px] md:text-[11.5px]" placeholder="a100" value={req.gpuType ?? ''} onChange={(e) => set({ gpuType: e.target.value || undefined })} /></label>
        <label className="flex flex-col gap-0.5 text-[10px] text-muted-foreground">CPUs
          <Input type="number" min={0} className="h-7 text-[11.5px] md:text-[11.5px]" value={req.cpu ?? ''} onChange={(e) => set({ cpu: num(e.target.value) })} /></label>
      </div>
    </Section>
  )
}

const CARD_TONE: Record<string, string> = {
  '1:1': 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300',
  '1:N': 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
  'N:1': 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
  'N:M': 'bg-rose-100 text-rose-700 dark:bg-rose-500/15 dark:text-rose-300',
  unknown: 'bg-muted text-muted-foreground',
}

// Join hints (catalog-driven): the backend suggests key columns for the two inputs, with the join
// cardinality MEASURED from the data. Clicking a suggestion fills the join's `on`/`condition`. A
// non-1:1 join gets a fan-out warning (the result lands at the finer grain — rows multiply).
function JoinHints({ nodeId }: { nodeId: string }) {
  const doc = useStore((s) => s.doc)
  const updateConfig = useStore((s) => s.updateConfig)
  const [analysis, setAnalysis] = useState<JoinAnalysis | null>(null)
  const [loading, setLoading] = useState(true)  // first analysis is pending → show 'Analyzing…', not 'no matches'
  // re-analyze when the graph shape or any node's config changes (debounced); positions don't matter
  const sig = JSON.stringify([doc.edges.map((e) => [e.source, e.target, e.targetHandle]),
    doc.nodes.map((n) => [n.id, n.type, n.data.config])])
  useEffect(() => {
    let off = false
    const t = setTimeout(() => {
      setLoading(true)
      api.joinAnalysis(doc, nodeId)
        .then((a) => { if (!off) setAnalysis(a) })
        .catch(() => { if (!off) setAnalysis(null) })
        .finally(() => { if (!off) setLoading(false) })
    }, 300)
    return () => { off = true; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, sig])

  const apply = (s: JoinSuggestion) => {
    const same = s.leftColumns.length === s.rightColumns.length && s.leftColumns.every((c, i) => c === s.rightColumns[i])
    if (same) updateConfig(nodeId, { on: s.leftColumns.join(', '), condition: '' })
    else updateConfig(nodeId, { condition: s.leftColumns.map((c, i) => `a.${c} = b.${s.rightColumns[i]}`).join(' AND '), on: '' })
  }

  const suggestions = analysis?.suggestions ?? []
  return (
    <Section title="Join hints">
      {analysis?.warning && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-[10.5px] leading-relaxed text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300">
          ⚠ {analysis.warning}
        </div>
      )}
      {suggestions.length === 0 ? (
        <div className="text-[10.5px] leading-relaxed text-muted-foreground">
          {loading ? 'Analyzing keys…' : (analysis?.note ?? 'No matching key columns between the two inputs.')}
        </div>
      ) : (
        <div className="flex flex-col gap-1">
          {suggestions.slice(0, 6).map((s, i) => (
            <button key={i} onClick={() => apply(s)} title={`${s.reason} · apply to the join`}
              className="flex items-center gap-2 rounded-md border border-border px-2 py-1.5 text-left hover:bg-accent">
              <span className="dp-mono flex-1 truncate text-[10.5px] text-foreground">
                {s.leftColumns.join('+')} = {s.rightColumns.join('+')}
              </span>
              <span className={cn('rounded px-1.5 py-px text-[9.5px] font-semibold', CARD_TONE[s.cardinality] ?? CARD_TONE.unknown)}>{s.cardinality}</span>
            </button>
          ))}
          <div className="text-[9.5px] leading-relaxed text-muted-foreground">Cardinality measured from the data · click to fill the join key.</div>
        </div>
      )}
    </Section>
  )
}

// Run-plan preview: the regions this node's run splits into, each with its backend, boundary tier, and
// estimated size. Self-hides for the trivial case (one local region) — it lights up only when placement
// did something (a cluster backend, an engine=ray label, or a checkpoint), so the scheduler is legible.
type PlanRegion = { id: string; outputNode: string; backend: string; tier: string | null; rows: number | null; confidence: string; requires?: string; unsatisfied?: boolean; available?: string; preflight?: string[] }
function RunPlan({ nodeId }: { nodeId: string }) {
  const doc = useStore((s) => s.doc)
  const kernelUp = useStore((s) => s.kernelUp)
  const [regions, setRegions] = useState<PlanRegion[] | null>(null)
  const sig = JSON.stringify([doc.edges.map((e) => [e.source, e.target, e.targetHandle]),
    doc.nodes.map((n) => [n.id, n.type, n.data.config])])
  useEffect(() => {
    if (!kernelUp) { setRegions(null); return }
    let off = false
    const t = setTimeout(() => {
      api.plan(doc, nodeId).then((p) => { if (!off) setRegions(p.regions ?? []) }).catch(() => { if (!off) setRegions(null) })
    }, 350)
    return () => { off = true; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, sig, kernelUp])

  // trivial = a single region on the local/default backend with no unmet requirement → nothing worth
  // showing (the card already shows ~N rows). Surface when placement split (>1), routed off-local, or a
  // resource requirement went unsatisfied (a pre-flight "this won't fit here" before you run).
  if (!regions || (regions.length <= 1 && regions.every((r) => r.backend === 'default' && !r.unsatisfied && !(r.preflight && r.preflight.length)))) return null
  const fmt = (n: number | null) => (n == null ? '?' : n.toLocaleString())
  const multi = regions.length > 1
  return (
    <Section title="Run plan">
      <div className="mb-1 text-[10.5px] leading-relaxed text-muted-foreground">
        {multi ? `This run splits into ${regions.length} regions — each runs on its backend, handing off via a tier.`
          : 'Placement for this run.'}
      </div>
      <div className="flex flex-col gap-1">
        {regions.map((r, i) => (
          <div key={r.id} className={cn('flex flex-wrap items-center gap-2 rounded-md border px-2 py-1 text-[10.5px]',
            r.unsatisfied ? 'border-amber-300 dark:border-amber-500/40' : 'border-border')}>
            <span className={cn('rounded px-1.5 py-px text-[9.5px] font-semibold',
              r.backend === 'default' ? 'bg-muted text-muted-foreground' : 'bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300')}>
              {r.backend === 'default' ? 'local' : r.backend}
            </span>
            <span className="dp-mono flex-1 truncate text-foreground">{r.outputNode}</span>
            <span className="tabular-nums text-muted-foreground">{r.confidence === 'unknown' ? '' : `~${fmt(r.rows)}`}</span>
            {multi && i < regions.length - 1 && r.tier && (
              <span className="rounded bg-muted px-1.5 py-px text-[9px] text-muted-foreground" title="materialization tier for the handoff">→ {r.tier}</span>
            )}
            {r.unsatisfied && (
              <span className="w-full text-[10px] text-amber-700 dark:text-amber-300" title="no registered backend satisfies this — it will run locally, which may lack the resource">
                ⚠ needs {r.requires || 'resources'} — {r.available || 'no backend provides it'}
              </span>
            )}
            {(r.preflight ?? []).map((w, j) => (
              <span key={j} className="w-full text-[10px] text-amber-700 dark:text-amber-300" title="source pre-flight — checked before the full run">⚠ {w}</span>
            ))}
          </div>
        ))}
      </div>
    </Section>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2 border-b border-border px-3.5 py-3">
      <div className="text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">{title}</div>
      {children}
    </div>
  )
}

// a port's display name: only real labels (join left/right, metric value) are named; the plain
// default in/out ports are nameless — their wire type is the meaningful label.
function portName(p: { id: string; label?: string }): string | null {
  if (p.label && p.label !== p.id) return p.label
  return p.id === 'in' || p.id === 'out' ? null : p.id
}

export function PortRow({ dir, name, wire, schema }: {
  dir: 'in' | 'out'; name: string | null; wire: string; schema?: ColumnSchema[] | null
}) {
  const [open, setOpen] = useState(false)
  const cols = Array.isArray(schema) ? schema : null
  const badge = schema === undefined ? null : cols === null ? 'untyped' : `${cols.length} cols`
  const expandable = !!cols && cols.length > 0
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center gap-[7px]">
        <span className="w-[26px] text-[8.5px] font-bold tracking-[0.4px] text-muted-foreground">{dir === 'in' ? 'IN' : 'OUT'}</span>
        {name && <span className="text-foreground">{name}</span>}
        <span className="flex-1 text-[10.5px] text-muted-foreground">{wire}</span>
        {badge && (
          <button type="button" disabled={!expandable} onClick={() => setOpen((o) => !o)}
            title={expandable ? (open ? 'Hide columns' : 'Show columns') : undefined}
            className={cn('rounded px-1.5 py-px text-[9.5px]',
              cols === null ? 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300'
                : 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300',
              expandable && 'cursor-pointer hover:opacity-80')}>
            {badge}
          </button>
        )}
      </div>
      {open && expandable && (
        <div className="ml-[33px] flex flex-col gap-px rounded border border-border bg-muted/40 p-1">
          {cols!.map((c, i) => (
            <div key={i} className="flex items-baseline justify-between gap-2 text-[10px]">
              <span className="dp-mono truncate text-foreground">{c.name}</span>
              <span className="dp-mono flex-none text-muted-foreground">{c.type}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Schema contract for a code op (transform / plugin / vector-search): untyped until it runs. The user
// can DECLARE the output columns (types this port + everything downstream via a typed stand-in) or
// INFER them from a bounded sample run. Both write config.outputSchema — declaring is just the manual
// path, inferring auto-fills it. Clearing it returns the port to untyped (dynamic) — all fine.
function SchemaContract({ nodeId, runnable }: { nodeId: string; runnable: boolean }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const cfg = (node?.data.config ?? {}) as Record<string, unknown>
  const declared = (Array.isArray(cfg.outputSchema) ? cfg.outputSchema : []) as ColumnSchema[]
  // outputSchema can instead be {ref: name} → this node REFERENCES a named workspace contract
  const os = cfg.outputSchema as { ref?: string } | undefined
  const refName = os && !Array.isArray(os) && typeof os === 'object' ? os.ref : undefined
  const enforce = !!cfg.enforceSchema
  const source = cfg.outputSchemaSource as string | undefined
  const code = cfg.code == null ? null : String(cfg.code)  // the cell this contract describes (transform)
  // the contract may be stale if the cell changed since it was pinned (only meaningful for a code cell)
  const stale = declared.length > 0 && code != null && !!cfg.outputSchemaCodeHash
    && cfg.outputSchemaCodeHash !== codeHash(code)
  const [names, setNames] = useState<string[]>([])       // named contracts available to reference
  const [refCols, setRefCols] = useState<ColumnSchema[]>([])
  useEffect(() => { api.listSchemas().then((s) => setNames(s.map((x) => x.name))).catch(() => {}) }, [])
  useEffect(() => {
    if (!refName) { setRefCols([]); return }
    api.listSchemas().then((s) => setRefCols(s.find((x) => x.name === refName)?.columns ?? [])).catch(() => setRefCols([]))
  }, [refName])
  const setEnforce = (on: boolean) => updateConfig(nodeId, { enforceSchema: on || undefined })
  const reference = (name: string) => updateConfig(nodeId, { outputSchema: name ? { ref: name } : undefined, outputSchemaSource: undefined, outputSchemaCodeHash: undefined })
  const saveAsNamed = async () => {
    const name = window.prompt('Save these columns as a named contract:')?.trim()
    if (!name) return
    try { await api.saveSchema(name, declared); setNames((n) => Array.from(new Set([...n, name]))) }
    catch (e) { setErr(e instanceof Error ? e.message : 'save failed') }
  }

  // a manual edit (no explicit src) takes ownership → 'declared'; only "Infer from sample" sets 'inferred'.
  // pin the current cell's hash alongside, so a later cell edit can flag the contract as possibly stale.
  const commit = (cols: ColumnSchema[], src: 'declared' | 'inferred' = 'declared') =>
    updateConfig(nodeId, {
      outputSchema: cols.length ? cols : undefined,
      outputSchemaSource: cols.length ? src : undefined,
      outputSchemaCodeHash: cols.length && code != null ? codeHash(code) : undefined,
    })

  const infer = async () => {
    setBusy(true); setErr(null)
    try {
      const res = await api.preview(useStore.getState().doc, nodeId, 50, 0)
      if (res.error || res.notPreviewable) setErr(res.reason || 'could not infer — run needs a full pass')
      else if (res.columns?.length) commit(res.columns as ColumnSchema[], 'inferred')
      else setErr('no columns produced on the sample')
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'infer failed')
    } finally { setBusy(false) }
  }

  return (
    <Section title="Output schema (contract)">
      {refName ? (
        <>
          <div className="text-[10.5px] leading-relaxed text-muted-foreground">
            References the named contract <span className="dp-mono text-foreground">{refName}</span> — shared across pipelines; edit it in the schema registry.
          </div>
          {refCols.map((c, i) => (
            <div key={i} className="flex items-center gap-2 text-[11px]">
              <span className="dp-mono min-w-0 flex-1 overflow-hidden text-ellipsis text-foreground">{c.name}</span>
              <span className="dp-mono w-[80px] flex-none text-muted-foreground">{c.type}</span>
            </div>
          ))}
          <div className="mt-0.5 flex flex-wrap items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => reference('')}
              className="h-auto px-2 py-1 text-[10.5px] font-medium text-muted-foreground shadow-none">Unlink</Button>
          </div>
        </>
      ) : (
        <>
          <div className="text-[10.5px] leading-relaxed text-muted-foreground">
            {declared.length
              ? (source === 'inferred' ? 'Inferred from a sample — edit to pin it as the contract.' : 'Declared — types this port and everything downstream.')
              : 'Untyped until it runs. Declare a contract, infer it, or reference a named one. Leave empty to stay dynamic.'}
          </div>
          {stale && (
            <div className="rounded-md border border-amber-300 bg-amber-50 px-2 py-1 text-[10px] leading-relaxed text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300">
              ⚠ The cell changed since this contract was pinned — it may be stale. Re-infer or edit to re-pin.
            </div>
          )}
          {declared.map((c, i) => (
            <div key={i} className="flex items-center gap-1">
              <Input value={c.name} placeholder="column"
                onChange={(e) => commit(declared.map((x, j) => (j === i ? { ...x, name: e.target.value.replace(/\s+/g, '_') } : x)))}
                className={cn(miniInputClass, 'dp-mono min-w-0 flex-1 text-[11px] md:text-[11px]')} />
              <Input value={c.type} placeholder="type"
                onChange={(e) => commit(declared.map((x, j) => (j === i ? { ...x, type: e.target.value } : x)))}
                className={cn(miniInputClass, 'dp-mono w-[80px] flex-none text-[11px] md:text-[11px]')} />
              <Button variant="ghost" size="icon" onClick={() => commit(declared.filter((_, j) => j !== i))} title="Remove column"
                className="h-5 w-5 flex-none text-muted-foreground [&_svg]:size-3"><Icon name="close" size={11} /></Button>
            </div>
          ))}
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
            <Button variant="outline" size="sm"
              onClick={() => commit([...declared, { name: `col${declared.length + 1}`, type: 'string', capabilities: [] }])}
              className="h-auto gap-1 self-start border-dashed px-2 py-1 text-[10.5px] font-medium text-muted-foreground shadow-none [&_svg]:size-3">
              <Icon name="plus" size={11} /> add column
            </Button>
            <Button variant="outline" size="sm" disabled={busy || !runnable} onClick={infer}
              title={runnable ? 'Run a bounded sample to resolve the output columns' : 'Wire a runnable input first'}
              className="h-auto gap-1 px-2 py-1 text-[10.5px] font-medium text-primary shadow-none [&_svg]:size-3">
              <Icon name="eye" size={11} /> {busy ? 'Inferring…' : 'Infer from sample'}
            </Button>
            {declared.length > 0 && (
              <Button variant="ghost" size="sm" onClick={saveAsNamed}
                className="h-auto px-2 py-1 text-[10.5px] font-medium text-primary shadow-none" title="Save these columns as a named, versioned workspace contract">Save as named…</Button>
            )}
            {declared.length > 0 && (
              <Button variant="ghost" size="sm" onClick={() => commit([])}
                className="h-auto px-2 py-1 text-[10.5px] font-medium text-muted-foreground shadow-none">Clear</Button>
            )}
          </div>
          {names.length > 0 && (
            <select value="" onChange={(e) => e.target.value && reference(e.target.value)}
              className={cn(miniInputClass, 'mt-1 text-[10.5px] text-muted-foreground')} title="Reference a named workspace contract">
              <option value="">Reference a named contract…</option>
              {names.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          )}
        </>
      )}
      {(declared.length > 0 || refName) && (
        <label className="mt-1 flex items-center gap-1.5 text-[10.5px] text-muted-foreground" title="Fail the run if the actual output columns drift from this contract (missing / unexpected / retyped)">
          <input type="checkbox" checked={enforce} onChange={(e) => setEnforce(e.target.checked)} /> Enforce (fail the run on drift)
        </label>
      )}
      {err && <div className="text-[10px] leading-relaxed text-amber-700 dark:text-amber-300">⚠ {err}</div>}
    </Section>
  )
}

function Action({ icon, label, onClick, disabled, danger }: { icon: IconName; label: string; onClick: () => void; disabled?: boolean; danger?: boolean }) {
  return (
    <Button
      variant="outline" size="sm"
      onClick={() => { if (!disabled) onClick() }}
      aria-disabled={disabled}
      className={cn(
        'h-auto gap-1.5 px-2 py-1.5 text-[11.5px] font-medium shadow-none [&_svg]:size-3',
        danger ? 'text-destructive' : 'text-muted-foreground',
        disabled && 'cursor-not-allowed opacity-50',
      )}>
      <Icon name={icon} size={12} /> {label}
    </Button>
  )
}
