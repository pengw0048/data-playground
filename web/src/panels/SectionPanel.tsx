import { Suspense, lazy, useState } from 'react'
import { roleCanEdit, useStore } from '../store/graph'
import { color } from '../theme/tokens'

const CodeEditor = lazy(() => import('../ui/CodeEditor').then((m) => ({ default: m.CodeEditor })))

// Editor for a `section` node: the driver script + its parentId-contained canvas nodes + params and a
// maxRuns bound. The script calls contained nodes by title: run(alias, data=…, output_port=…, **cfg).
export function SectionPanel({ nodeId }: { nodeId: string }) {
  const nodes = useStore((s) => s.doc.nodes) // stable ref; filter in-body (a filtering selector returns a new array each render → infinite loop)
  const node = nodes.find((n) => n.id === nodeId)
  const children = nodes.filter((n) => n.parentId === nodeId)
  const updateConfig = useStore((s) => s.updateConfig)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  if (!node) return null
  const cfg = node.data.config
  const outputs = (Array.isArray(cfg.outputs) && cfg.outputs.length ? cfg.outputs : ['out']) as string[]
  // outputs: the section's output ports. `emit(rel)` fills "out"; `emit("name", rel)` a named port.
  const setOutputs = (v: string) => {
    const ports = [...new Set(v.split(',').map((s) => s.trim()).filter(Boolean))]  // de-dup: unique handles
    updateConfig(nodeId, { outputs: ports.length ? ports : ['out'] })
  }

  return (
    <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ fontSize: 11, color: color.text3, lineHeight: 1.5 }}>
        A driver script over the contained nodes. Call a node by alias: <code>run(alias, data=inputs['in'], output_port='port', **cfg)</code>;
        choose <code>output_port</code> when that child has multiple outputs;
        read a scalar with <code>value(...)</code>; <code>concat([...])</code>; return results with <code>emit(rel)</code> or,
        for multiple output ports, <code>emit("port", rel)</code>. Loops are bounded by maxRuns. Not sample-previewable — runs on a full pass.
      </div>

      {!canEdit && <div className="rounded-md bg-muted px-2.5 py-1.5 text-[10.5px] text-muted-foreground">View-only access</div>}

      <fieldset disabled={!canEdit} style={{ display: 'contents' }}>

      <Field label="driver script (Python)">
        <Suspense fallback={<div style={{ height: 200, border: `1px solid ${color.border}`, borderRadius: 8, display: 'grid', placeItems: 'center', color: color.text3, fontSize: 12 }}>loading editor…</div>}>
          <CodeEditor language="python" height={200} value={String(cfg.script ?? '')} readOnly={!canEdit} onChange={(v) => updateConfig(nodeId, { script: v })} />
        </Suspense>
      </Field>

      <Field label="contained nodes (on the canvas)">
        {children.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div style={{ fontSize: 11, color: color.text3, lineHeight: 1.5 }}>
              Drop nodes onto the section frame to contain them. The script calls each by its title:
            </div>
            {children.map((c) => (
              <div key={c.id} style={{ display: 'flex', alignItems: 'center', gap: 8, border: `1px solid ${color.border}`, borderRadius: 8, padding: '6px 9px', fontSize: 12 }}>
                <code>run(&apos;{c.data.title}&apos;, …)</code>
                <span style={{ flex: 1 }} />
                <span style={{ fontSize: 10, color: color.text3 }}>{c.type}</span>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ fontSize: 11, color: color.text3, lineHeight: 1.5 }}>
            Drop nodes onto the section frame to make them callable from the driver script.
          </div>
        )}
      </Field>

      <Field label="output ports (comma-separated)">
        <input defaultValue={outputs.join(', ')} onChange={(e) => setOutputs(e.target.value)} placeholder="out"
          className="dp-mono"
          style={{ width: '100%', fontSize: 11.5, border: `1px solid ${color.border}`, borderRadius: 6, padding: '5px 8px', outline: 'none' }} />
      </Field>

      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ flex: 1 }}><JsonField label="params (JSON)" value={(cfg.params as object) ?? {}} onChange={(v) => updateConfig(nodeId, { params: v as Record<string, unknown> })} /></div>
        <Field label="maxRuns">
          <input type="number" value={Number(cfg.maxRuns ?? 200)} onChange={(e) => updateConfig(nodeId, { maxRuns: parseInt(e.target.value, 10) || 1 })}
            style={{ width: 90, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 6, padding: '5px 8px', outline: 'none' }} />
        </Field>
      </div>
      </fieldset>
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
