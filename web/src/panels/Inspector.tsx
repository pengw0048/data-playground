import { useEffect, useState } from 'react'
import { useStore, nodeRunnable } from '../store/graph'
import { getSpec } from '../nodes/registry'
import { getBackendSpec, NodeParamFields, nodeInvalidReason } from '../nodes/generic'
import { color, radius, status as statusTok, kindAccent } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'

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
    <aside data-testid="inspector" style={{
      width: INSPECTOR_W, flex: `0 0 ${INSPECTOR_W}px`, height: '100%', background: '#fff',
      borderLeft: `1px solid ${color.border}`, display: 'flex', flexDirection: 'column', overflow: 'hidden',
    }}>
      <div style={{ height: 52, flex: '0 0 52px', borderBottom: `1px solid ${color.hairline}`, display: 'flex', alignItems: 'center', padding: '0 14px', fontSize: 13, fontWeight: 600, color: color.ink }}>
        Inspector
      </div>
      {node ? <NodeInspector key={node.id} nodeId={node.id} />
        : <Empty text={selectedIds.length > 1 ? `${selectedIds.length} nodes selected` : 'Select a node to see its properties'} />}
    </aside>
  )
}

function Empty({ text }: { text: string }) {
  return (
    <div style={{ flex: 1, display: 'grid', placeItems: 'center', padding: 24, textAlign: 'center', color: color.text3, fontSize: 12, lineHeight: 1.6 }}>
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
    <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column' }}>
      {/* header */}
      <div style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8, borderBottom: `1px solid ${color.hairline}` }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ width: 4, height: 26, borderRadius: 2, background: kindAccent[kind] ?? color.text3 }} />
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => { if (name.trim() && name !== node.data.title) rename(nodeId, name.trim()) }}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
            style={{ flex: 1, minWidth: 0, fontSize: 14, fontWeight: 600, color: color.ink, border: '1px solid transparent', borderRadius: 6, padding: '3px 6px', outline: 'none', background: 'transparent' }}
            onFocus={(e) => (e.currentTarget.style.borderColor = color.border)}
          />
          <span style={{ fontSize: 8.5, fontWeight: 600, letterSpacing: 0.6, color: color.text3, background: '#f1f2f4', padding: '2px 6px', borderRadius: radius.chip }}>
            {(spec?.tag ?? kind).toUpperCase()}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: color.text2 }}>
          <span style={{ color: st.color }}>{st.glyph}</span> {st.label}
          {spec?.blurb && <span style={{ color: color.text3 }}>· {spec.blurb}</span>}
        </div>
      </div>

      {/* properties (reused generic param editor) */}
      <Section title="Properties">
        <NodeParamFields nodeId={nodeId} />
        {codeParams.length === 0 && (bspec?.params ?? []).length === 0 && (
          <div style={{ fontSize: 11.5, color: color.text3 }}>No editable parameters.</div>
        )}
      </Section>

      {/* code snippet + open the full editor (Monaco panel; fullscreen editor is a later step) */}
      {codeParams.map((p) => {
        const codeText = String(cfg[p.name] ?? p.default ?? '')
        return (
          <Section key={p.name} title={p.label ?? p.name}>
            <pre className="dp-mono" style={{ margin: 0, maxHeight: 120, overflow: 'auto', fontSize: 10.5, lineHeight: 1.5, color: color.text2, background: 'var(--code-bg, #f7f8fa)', border: `1px solid ${color.border}`, borderRadius: 8, padding: 8, whiteSpace: 'pre' }}>
              {codeText || '(empty)'}
            </pre>
            <div style={{ marginTop: 6, display: 'flex', gap: 6 }}>
              {kind === 'section' ? (
                <CodeBtn icon="code" label="Open section editor →" onClick={() => togglePanel(nodeId, 'section')} />
              ) : (
                <CodeBtn icon="external" label="Open fullscreen editor" onClick={() => openCodeFullscreen(nodeId, p.name, p.lang)} />
              )}
            </div>
          </Section>
        )
      })}

      {/* ports — a real port label (join left/right, metric value) shows as a name; the default
          in/out ports just show their wire type. Outputs carry a typed/untyped schema badge. */}
      <Section title="Ports">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 11.5, color: color.text2 }}>
          {(spec?.inputs ?? []).map((p) => <PortRow key={`in-${p.id}`} dir="in" name={portName(p)} wire={p.wire} />)}
          {(spec?.outputs ?? []).map((p, i) => (
            <PortRow key={`out-${p.id}`} dir="out" name={portName(p)} wire={p.wire}
              schema={i === 0 ? (outSchema === undefined ? undefined : outSchema) : undefined} />
          ))}
          {(spec?.inputs ?? []).length === 0 && (spec?.outputs ?? []).length === 0 && <span style={{ color: color.text3 }}>—</span>}
        </div>
        {/* editable output ports: only on the section (its driver script emit()s to named ports) —
            fixed-port ops (filter/sort/join) keep their ports as a type contract the wires rely on */}
        {kind === 'section' && <OutputPortsEditor nodeId={nodeId} />}
      </Section>

      {/* actions */}
      <Section title="Actions">
        {invalid && <div style={{ fontSize: 11, color: '#a2731a', marginBottom: 6 }}>⚠ {invalid}</div>}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          <Action icon="eye" label="View data" disabled={!runnable || !!invalid} onClick={() => runPreview(nodeId)} />
          <Action icon={runState === 'running' ? 'stop' : 'play'} label={kind === 'source' ? 'Count rows' : runState === 'running' ? 'Stop' : 'Run'} disabled={(!runnable || !!invalid) && runState !== 'running'}
            onClick={() => (runState === 'running' ? cancelRun(nodeId) : requestRun(nodeId))} />
          {spec?.canBypass && <Action icon="power" label="Bypass" onClick={() => bypass(nodeId)} />}
          <Action icon="mute" label={node.data.disabled ? 'Enable' : 'Disable'} onClick={() => disable(nodeId)} />
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
  const outputs = useStore((s) => {
    const o = (s.doc.nodes.find((n) => n.id === nodeId)?.data.config as { outputs?: unknown }).outputs
    return Array.isArray(o) && o.length ? o.map(String) : ['out']
  })
  const updateConfig = useStore((s) => s.updateConfig)
  const commit = (next: string[]) => updateConfig(nodeId, { outputs: next })
  return (
    <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
      <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.4, color: color.text3 }}>OUTPUT PORTS (emit)</div>
      {outputs.map((name, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <input value={name} onChange={(e) => commit(outputs.map((x, j) => (j === i ? e.target.value.replace(/\s+/g, '_') : x)))}
            className="dp-mono" style={{ flex: 1, fontSize: 11, border: `1px solid ${color.border}`, borderRadius: 6, padding: '4px 7px', outline: 'none' }} />
          {outputs.length > 1 && (
            <button onClick={() => commit(outputs.filter((_, j) => j !== i))} title="Remove port"
              style={{ border: 'none', background: 'transparent', color: color.text3, cursor: 'pointer', display: 'grid', placeItems: 'center', width: 20, height: 20 }}><Icon name="close" size={11} /></button>
          )}
        </div>
      ))}
      <button onClick={() => commit([...outputs, `out${outputs.length + 1}`])}
        style={{ display: 'inline-flex', alignItems: 'center', gap: 4, alignSelf: 'flex-start', border: `1px dashed ${color.border}`, background: 'transparent', color: color.text3, fontSize: 10.5, padding: '4px 8px', borderRadius: radius.chip, cursor: 'pointer' }}>
        <Icon name="plus" size={11} /> add port
      </button>
    </div>
  )
}

function CodeBtn({ icon, label, onClick }: { icon: IconName; label: string; onClick: () => void }) {
  return (
    <button onClick={onClick}
      style={{ display: 'inline-flex', alignItems: 'center', gap: 5, border: `1px solid ${color.border}`, borderRadius: 7, background: '#fff', color: color.focus, fontSize: 11.5, padding: '5px 10px', cursor: 'pointer' }}>
      <Icon name={icon} size={12} /> {label}
    </button>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ padding: '12px 14px', borderBottom: `1px solid ${color.hairline}`, display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3 }}>{title}</div>
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
    <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
      <span style={{ fontSize: 8.5, fontWeight: 700, letterSpacing: 0.4, color: color.text3, width: 26 }}>{dir === 'in' ? 'IN' : 'OUT'}</span>
      {name && <span style={{ color: color.text2 }}>{name}</span>}
      <span style={{ flex: 1, fontSize: 10.5, color: color.text3 }}>{wire}</span>
      {badge && <span style={{ fontSize: 9.5, color: schema === null ? '#a2731a' : '#1f7a45', background: schema === null ? '#fbf1dc' : '#e3f3ea', padding: '1px 6px', borderRadius: radius.chip }}>{badge}</span>}
    </div>
  )
}

function Action({ icon, label, onClick, disabled, danger }: { icon: IconName; label: string; onClick: () => void; disabled?: boolean; danger?: boolean }) {
  return (
    <button
      onClick={() => { if (!disabled) onClick() }}
      aria-disabled={disabled}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 5, border: `1px solid ${color.border}`, borderRadius: 7,
        background: '#fff', color: disabled ? '#c8ccd2' : danger ? color.failed : color.text2,
        fontSize: 11.5, padding: '5px 9px', cursor: disabled ? 'not-allowed' : 'pointer',
      }}>
      <Icon name={icon} size={12} /> {label}
    </button>
  )
}
