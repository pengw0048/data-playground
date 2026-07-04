import { useEffect, useState } from 'react'
import { useStore } from '../store/graph'
import { api } from '../api/client'
import { color, kindAccent, shadow } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Segmented } from '../ui/controls'

// The agent is an actor (§5.8): you describe an outcome; it BUILDS real, inspectable typed nodes
// via a server-side tool-use loop. It requires a configured model (DP_AGENT_MODEL + a provider key,
// set in Settings — the key stays in the kernel, never the browser). With no model it is simply
// unavailable — no rule-based stand-in that pretends to be an LLM.
export function AgentDock() {
  const open = useStore((s) => s.agentOpen)
  const setOpen = useStore((s) => s.setAgentOpen)
  const mode = useStore((s) => s.agentMode)
  const setMode = useStore((s) => s.setAgentMode)
  const log = useStore((s) => s.agentLog)
  const push = useStore((s) => s.pushAgent)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [llm, setLlm] = useState<{ available: boolean; model?: string; reason?: string } | null>(null)

  useEffect(() => {
    if (!open) return
    api.agentStatus().then((s) => setLlm({ available: s.available, model: s.model, reason: s.reason })).catch(() => setLlm({ available: false, reason: 'kernel offline' }))
  }, [open])

  if (!open) return null

  const ready = !!llm?.available
  const submit = async () => {
    const intent = text.trim()
    if (!intent || busy || !ready) return
    push({ role: 'user', text: intent })
    setText('')
    setBusy(true)
    try {
      const { doc } = useStore.getState()
      const res = await api.agentAct(doc, intent)
      if (res.available && res.graph) {
        const built = (res.transcript ?? []).filter((t) => t.tool === 'add_node').map((t) => String(t.input.kind))
        push({ role: 'agent', text: res.summary || (mode === 'build' ? 'Built the pipeline.' : 'Proposed a pipeline.'), plan: built.length ? built : undefined })
        if (mode === 'build') {
          useStore.getState().applyAgentGraph(res.graph)
          runTerminal(res.graph)
        }
      } else {
        setLlm({ available: false, reason: res.reason })
        push({ role: 'agent', text: `Agent unavailable — ${res.reason ?? 'no model configured'}. Set a model in Settings.` })
      }
    } catch (e) {
      push({ role: 'agent', text: `Agent error: ${(e as Error).message}` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ position: 'absolute', left: '50%', bottom: 84, transform: 'translateX(-50%)', width: 420, zIndex: 24 }} className="dp-panel">
      <div style={{ background: '#fff', border: `1px solid ${color.border}`, borderRadius: 14, boxShadow: shadow.panel, overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px', borderBottom: `1px solid ${color.hairline}` }}>
          <span style={{ color: '#6b4bd6' }}><Icon name="sparkle" size={15} /></span>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Agent</span>
          <span style={{ fontSize: 10.5, color: ready ? color.text3 : '#a2731a', background: ready ? '#efeaff' : '#fbf1dc', padding: '2px 7px', borderRadius: 4 }}>
            {ready ? (llm?.model ?? 'llm') : 'unavailable'}
          </span>
          <span style={{ flex: 1 }} />
          <Segmented options={[{ value: 'plan', label: 'Plan' }, { value: 'build', label: 'Build' }]} value={mode} onChange={setMode} accent="#6b4bd6" />
          <button onClick={() => setOpen(false)} style={{ width: 24, height: 22, border: 'none', background: 'transparent', color: color.text3, display: 'grid', placeItems: 'center' }}><Icon name="close" size={13} /></button>
        </div>

        <div style={{ maxHeight: 260, overflowY: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 12 }}>
          {llm && !ready ? (
            <div style={{ fontSize: 11.5, color: color.text2, lineHeight: 1.6 }}>
              <b>Agent unavailable</b> — {llm.reason ?? 'no model configured'}.<br />
              Set a model in <b>Settings → Agent</b> (a provider key stays in the kernel, never the browser), then reopen this.
              <div style={{ marginTop: 10 }}>
                <button data-testid="agent-configure" onClick={() => window.dispatchEvent(new CustomEvent('dp-open-settings'))}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '6px 12px', border: 'none', borderRadius: 8, background: '#6b4bd6', color: '#fff', fontSize: 12, fontWeight: 600, cursor: 'pointer' }}>
                  <Icon name="settings" size={12} /> Configure a model
                </button>
              </div>
            </div>
          ) : log.length === 0 && (
            <div style={{ fontSize: 11.5, color: color.text3, lineHeight: 1.6 }}>
              Describe an outcome — e.g. <i>“sample images, filter where is_valid, write a table”</i>. Build creates real, inspectable nodes.
            </div>
          )}
          {log.map((m, i) => (
            <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 6, alignItems: m.role === 'user' ? 'flex-end' : 'flex-start' }}>
              <div style={{ fontSize: 12, lineHeight: 1.4, padding: '7px 11px', borderRadius: 10, maxWidth: '85%', background: m.role === 'user' ? '#eef0f3' : '#f3effe', color: color.ink, whiteSpace: 'pre-wrap' }}>
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
                </div>
              )}
            </div>
          ))}
          {busy && <div style={{ fontSize: 11.5, color: color.text3 }}>working…</div>}
        </div>

        <div style={{ display: 'flex', gap: 8, padding: 10, borderTop: `1px solid ${color.hairline}` }}>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
            placeholder={ready ? 'Describe an outcome…' : 'Configure a model to use the agent'}
            disabled={busy || !ready}
            style={{ flex: 1, fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 9, padding: '9px 11px', outline: 'none', opacity: (busy || !ready) ? 0.6 : 1 }}
          />
          <button data-testid="agent-submit" onClick={submit} disabled={busy || !ready} style={{ padding: '0 16px', border: 'none', borderRadius: 9, background: '#6b4bd6', color: '#fff', fontSize: 12.5, fontWeight: 600, opacity: (busy || !ready) ? 0.6 : 1, cursor: ready ? 'pointer' : 'not-allowed' }}>
            {mode === 'build' ? 'Build' : 'Plan'}
          </button>
        </div>
      </div>
    </div>
  )
}

// Run/preview the pipeline's terminal node so the agent "shows its work" after a build.
function runTerminal(bg: { nodes: { id: string; type: string }[]; edges: { source: string }[] }) {
  const sources = new Set(bg.edges.map((e) => e.source))
  const sink = [...bg.nodes].reverse().find((n) => !sources.has(n.id))
  if (!sink) return
  const store = useStore.getState()
  if (['write', 'opaque', 'loop'].includes(sink.type)) store.requestRun(sink.id)
  else store.runPreview(sink.id)
}

function stripTitle(p: string): string {
  const known = ['source', 'sample', 'filter', 'select', 'transform', 'join', 'sql', 'aggregate', 'sort', 'dedup', 'write', 'metric', 'section']
  return known.find((k) => p.includes(k)) ?? 'transform'
}
