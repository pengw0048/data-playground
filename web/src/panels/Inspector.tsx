import { useEffect, useState } from 'react'
import { useStore, nodeRunnable } from '../store/graph'
import { getSpec } from '../nodes/registry'
import { getBackendSpec, NodeParamFields, nodeInvalidReason } from '../nodes/generic'
import { color, status as statusTok, kindAccent } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { FileDialog } from '../ui/FileDialog'
import { miniInputClass } from '../ui/controls'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Separator } from '@/components/ui/separator'
import { cn } from '@/lib/utils'

export const INSPECTOR_W = 300

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
  const outSchema = useStore((s) => s.schemas[nodeId])  // ColumnSchema[] = typed · null = untyped
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

      {/* compute placement: what this step needs → the run routes to a matching worker (e.g. a GPU pool) */}
      {(kind === 'transform' || kind === 'section') && <ResourcesSection nodeId={nodeId} />}

      {/* ports — a real port label (join left/right, metric value) shows as a name; the default
          in/out ports just show their wire type. Outputs carry a typed/untyped schema badge. */}
      <Section title="Ports">
        <div className="flex flex-col gap-1 text-[11.5px] text-muted-foreground">
          {(spec?.inputs ?? []).map((p) => <PortRow key={`in-${p.id}`} dir="in" name={portName(p)} wire={p.wire} />)}
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

      {/* actions */}
      <Section title="Actions">
        {invalid && <div className="mb-1.5 text-[11px] text-amber-700">⚠ {invalid}</div>}
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

function PortRow({ dir, name, wire, schema }: {
  dir: 'in' | 'out'; name: string | null; wire: string; schema?: { name: string }[] | null
}) {
  const badge = schema === undefined ? null : schema === null ? 'untyped' : `${schema.length} cols`
  return (
    <div className="flex items-center gap-[7px]">
      <span className="w-[26px] text-[8.5px] font-bold tracking-[0.4px] text-muted-foreground">{dir === 'in' ? 'IN' : 'OUT'}</span>
      {name && <span className="text-foreground">{name}</span>}
      <span className="flex-1 text-[10.5px] text-muted-foreground">{wire}</span>
      {badge && <span className={cn('rounded px-1.5 py-px text-[9.5px]', schema === null ? 'bg-amber-100 text-amber-700' : 'bg-green-100 text-green-700')}>{badge}</span>}
    </div>
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
