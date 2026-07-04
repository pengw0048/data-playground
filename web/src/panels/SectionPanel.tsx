import { Suspense, lazy, useState } from 'react'
import { useStore } from '../store/graph'
import { allSpecs } from '../nodes/registry'
import { color } from '../theme/tokens'
import { Icon } from '../ui/Icon'

const CodeEditor = lazy(() => import('../ui/CodeEditor').then((m) => ({ default: m.CodeEditor })))

interface SubNode { alias: string; type: string; config: Record<string, unknown> }

// Editor for a `section` node: the driver script + the nodes it contains (alias/type/config) +
// params + a maxRuns bound. The script calls the sub-nodes by alias: run(alias, data=…, **cfg).
export function SectionPanel({ nodeId }: { nodeId: string }) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const updateConfig = useStore((s) => s.updateConfig)
  if (!node) return null
  const cfg = node.data.config
  const subnodes = (Array.isArray(cfg.subnodes) ? cfg.subnodes : []) as SubNode[]
  const kinds = allSpecs().map((s) => s.kind).filter((k) => k !== 'section' && k !== 'note')

  const setSubs = (next: SubNode[]) => updateConfig(nodeId, { subnodes: next })
  const patchSub = (i: number, patch: Partial<SubNode>) => setSubs(subnodes.map((s, j) => (j === i ? { ...s, ...patch } : s)))

  return (
    <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ fontSize: 11, color: color.text3, lineHeight: 1.5 }}>
        A driver script over the contained nodes. Call a node by alias: <code>run(alias, data=inputs['in'], **cfg)</code>;
        read a scalar with <code>value(...)</code>; <code>concat([...])</code>; return with <code>emit(...)</code>.
        Loops are bounded by maxRuns. Not sample-previewable — runs on a full pass.
      </div>

      <Field label="driver script (Python)">
        <Suspense fallback={<div style={{ height: 200, border: `1px solid ${color.border}`, borderRadius: 8, display: 'grid', placeItems: 'center', color: color.text3, fontSize: 12 }}>loading editor…</div>}>
          <CodeEditor language="python" height={200} value={String(cfg.script ?? '')} onChange={(v) => updateConfig(nodeId, { script: v })} />
        </Suspense>
      </Field>

      <Field label="contained nodes">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {subnodes.map((s, i) => (
            <div key={i} style={{ border: `1px solid ${color.border}`, borderRadius: 8, padding: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <input value={s.alias} placeholder="alias" onChange={(e) => patchSub(i, { alias: e.target.value })}
                  style={{ width: 120, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 6, padding: '5px 8px', outline: 'none' }} />
                <select value={s.type} onChange={(e) => patchSub(i, { type: e.target.value })}
                  style={{ flex: 1, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 6, padding: '5px 8px', background: '#fff' }}>
                  {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
                </select>
                <button onClick={() => setSubs(subnodes.filter((_, j) => j !== i))} title="remove"
                  style={{ border: 'none', background: 'transparent', color: color.text3, cursor: 'pointer' }}><Icon name="close" size={13} /></button>
              </div>
              <JsonField label="config" value={s.config ?? {}} onChange={(v) => patchSub(i, { config: v as Record<string, unknown> })} />
            </div>
          ))}
          <button
            onClick={() => setSubs([...subnodes, { alias: `n${subnodes.length + 1}`, type: kinds[0] ?? 'filter', config: {} }])}
            style={{ alignSelf: 'flex-start', display: 'inline-flex', alignItems: 'center', gap: 5, border: `1px solid ${color.border}`, borderRadius: 7, background: '#fff', color: color.text2, fontSize: 12, padding: '6px 10px' }}>
            <Icon name="plus" size={12} /> <span>add node</span>
          </button>
        </div>
      </Field>

      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ flex: 1 }}><JsonField label="params (JSON)" value={(cfg.params as object) ?? {}} onChange={(v) => updateConfig(nodeId, { params: v as Record<string, unknown> })} /></div>
        <Field label="maxRuns">
          <input type="number" value={Number(cfg.maxRuns ?? 200)} onChange={(e) => updateConfig(nodeId, { maxRuns: parseInt(e.target.value, 10) || 1 })}
            style={{ width: 90, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 6, padding: '5px 8px', outline: 'none' }} />
        </Field>
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'block' }}>
      <div style={{ fontSize: 11, color: color.text2, marginBottom: 5, fontWeight: 600 }}>{label}</div>
      {children}
    </label>
  )
}

function JsonField({ label, value, onChange }: { label: string; value: object; onChange: (v: unknown) => void }) {
  const [text, setText] = useState(JSON.stringify(value ?? {}, null, 0))
  const [bad, setBad] = useState(false)
  return (
    <div>
      <div style={{ fontSize: 10.5, color: color.text3, marginBottom: 3 }}>{label}</div>
      <input
        value={text}
        onChange={(e) => {
          setText(e.target.value)
          try { onChange(JSON.parse(e.target.value || '{}')); setBad(false) } catch { setBad(true) }
        }}
        spellCheck={false}
        className="dp-mono"
        style={{ width: '100%', fontSize: 11, border: `1px solid ${bad ? color.failed : color.border}`, borderRadius: 6, padding: '5px 8px', outline: 'none' }}
      />
    </div>
  )
}
