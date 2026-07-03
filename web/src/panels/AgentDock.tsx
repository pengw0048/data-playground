import { useState } from 'react'
import { useReactFlow } from '@xyflow/react'
import { useStore, newId } from '../store/graph'
import { plan, type PlanStep } from '../agent/planner'
import { portWire, canConnect } from '../nodes/registry'
import { color, kindAccent, radius, shadow } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Segmented } from '../ui/controls'

// The agent is an actor (§5.8): you describe an outcome; it PLANS typed node steps and, in
// Build mode, materializes real, inspectable nodes on the canvas and runs them.
export function AgentDock() {
  const open = useStore((s) => s.agentOpen)
  const setOpen = useStore((s) => s.setAgentOpen)
  const mode = useStore((s) => s.agentMode)
  const setMode = useStore((s) => s.setAgentMode)
  const log = useStore((s) => s.agentLog)
  const push = useStore((s) => s.pushAgent)
  const { screenToFlowPosition } = useReactFlow()
  const [text, setText] = useState('')

  if (!open) return null

  const submit = () => {
    const intent = text.trim()
    if (!intent) return
    push({ role: 'user', text: intent })
    setText('')
    const { catalog, doc } = useStore.getState()
    const hasSource = doc.nodes.some((n) => n.type === 'source')
    const { steps } = plan(intent, catalog, hasSource)
    if (steps.length === 0) {
      push({ role: 'agent', text: 'I couldn’t turn that into a pipeline. Name a dataset and an action — e.g. “sample images, filter where is_valid, write a table”.' })
      return
    }
    push({ role: 'agent', text: mode === 'build' ? `Built ${steps.length} nodes.` : 'Plan (nothing ran):', plan: steps.map((s) => s.title ?? s.kind) })
    if (mode === 'build') build(steps, screenToFlowPosition)
  }

  return (
    <div style={{ position: 'absolute', left: '50%', bottom: 84, transform: 'translateX(-50%)', width: 420, zIndex: 24 }} className="dp-panel">
      <div style={{ background: '#fff', border: `1px solid ${color.border}`, borderRadius: 14, boxShadow: shadow.panel, overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px', borderBottom: `1px solid ${color.hairline}` }}>
          <span style={{ color: '#6b4bd6' }}><Icon name="sparkle" size={15} /></span>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Agent</span>
          <span style={{ fontSize: 10.5, color: color.text3, background: '#efeaff', padding: '2px 7px', borderRadius: 4 }}>does, not just asks</span>
          <span style={{ flex: 1 }} />
          <Segmented options={[{ value: 'plan', label: 'Plan' }, { value: 'build', label: 'Build' }]} value={mode} onChange={setMode} accent="#6b4bd6" />
          <button onClick={() => setOpen(false)} style={{ width: 24, height: 22, border: 'none', background: 'transparent', color: color.text3, display: 'grid', placeItems: 'center' }}><Icon name="close" size={13} /></button>
        </div>

        <div style={{ maxHeight: 260, overflowY: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 12 }}>
          {log.length === 0 && (
            <div style={{ fontSize: 11.5, color: color.text3, lineHeight: 1.6 }}>
              Describe an outcome — e.g. <i>“sample images, filter where is_valid, write a table”</i>. Build creates real, inspectable nodes.
            </div>
          )}
          {log.map((m, i) => (
            <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 6, alignItems: m.role === 'user' ? 'flex-end' : 'flex-start' }}>
              <div style={{ fontSize: 12, lineHeight: 1.4, padding: '7px 11px', borderRadius: 10, maxWidth: '85%', background: m.role === 'user' ? '#eef0f3' : '#f3effe', color: color.ink }}>
                {m.text}
              </div>
              {m.plan && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, alignItems: 'center' }}>
                  {m.plan.map((p, j) => (
                    <span key={j} style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 10.5, fontWeight: 600, background: '#f1f2f4', padding: '3px 8px', borderRadius: 5, color: color.text2 }}>
                        <span style={{ width: 6, height: 6, borderRadius: '50%', background: kindAccent[stripTitle(p)] ?? color.text3 }} />
                        {p}
                      </span>
                      {j < m.plan!.length - 1 && <Icon name="chevronRight" size={11} style={{ color: color.text3 }} />}
                    </span>
                  ))}
                  {mode === 'build' && <span style={{ color: color.latest, marginLeft: 3 }}><Icon name="check" size={13} /></span>}
                </div>
              )}
            </div>
          ))}
        </div>

        <div style={{ display: 'flex', gap: 8, padding: 10, borderTop: `1px solid ${color.hairline}` }}>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
            placeholder="Describe an outcome…"
            style={{ flex: 1, fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 9, padding: '9px 11px', outline: 'none' }}
          />
          <button onClick={submit} style={{ padding: '0 16px', border: 'none', borderRadius: 9, background: '#6b4bd6', color: '#fff', fontSize: 12.5, fontWeight: 600 }}>
            {mode === 'build' ? 'Build' : 'Plan'}
          </button>
        </div>
      </div>
    </div>
  )
}

function stripTitle(p: string): string {
  // best-effort: map a plan label back to a kind for the dot color
  const known = ['source', 'sample', 'filter', 'transform', 'join', 'sql', 'write', 'metric', 'notebook', 'branch', 'loop', 'variable', 'opaque']
  return known.find((k) => p.includes(k)) ?? 'transform'
}

function build(steps: PlanStep[], screenToFlow: (p: { x: number; y: number }) => { x: number; y: number }) {
  const store = useStore.getState()
  // place the new chain below existing content so it never overlaps
  const existing = store.doc.nodes
  const origin = existing.length
    ? { x: Math.min(...existing.map((n) => n.position.x)), y: Math.max(...existing.map((n) => n.position.y)) + 280 }
    : screenToFlow({ x: 240, y: 240 })
  let prevId: string | null = null
  let prevKind: string | null = null
  steps.forEach((s, i) => {
    const node = store.addNode(s.kind, { x: origin.x + i * 268, y: origin.y }, s.config, s.title)
    if (!node) return
    if (prevId && prevKind) {
      const sw = portWire(useStore.getState().doc.nodes, prevId, null, 'source')
      if (sw && canConnect(sw, s.kind, null)) {
        store.connect({ id: newId('e'), source: prevId, target: node.id, sourceHandle: null, targetHandle: null, data: { wire: sw } })
      }
    }
    prevId = node.id
    prevKind = s.kind
  })
  // run the terminal node so the agent "shows its work"
  const last = useStore.getState().doc.nodes.slice(-1)[0]
  if (last) {
    if (last.type === 'write' || last.type === 'opaque' || last.type === 'loop') store.requestRun(last.id)
    else store.runPreview(last.id)
  }
}
