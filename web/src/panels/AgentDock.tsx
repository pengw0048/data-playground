import { useEffect, useState } from 'react'
import { useStore } from '../store/graph'
import { api } from '../api/client'
import { color, kindAccent } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Segmented } from '../ui/controls'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/input'

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
    <div className="dp-panel absolute bottom-[84px] left-1/2 z-[24] w-[420px] -translate-x-1/2">
      <div className="overflow-hidden rounded-[14px] border border-border bg-card shadow-lg">
        <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
          <span className="flex items-center text-[#6b4bd6]"><Icon name="sparkle" size={15} /></span>
          <span className="text-[13px] font-semibold text-foreground">Agent</span>
          <span className={cn('rounded px-[7px] py-0.5 text-[10.5px]', ready ? 'bg-[#6b4bd6]/10 text-muted-foreground' : 'bg-[#fbf1dc] text-[#a2731a]')}>
            {ready ? (llm?.model ?? 'llm') : 'unavailable'}
          </span>
          <span className="flex-1" />
          <Segmented options={[{ value: 'plan', label: 'Plan' }, { value: 'build', label: 'Build' }]} value={mode} onChange={setMode} accent="#6b4bd6" />
          <button onClick={() => setOpen(false)} className="grid h-[22px] w-6 place-items-center border-0 bg-transparent text-muted-foreground hover:text-foreground"><Icon name="close" size={13} /></button>
        </div>

        <div className="flex max-h-[260px] flex-col gap-3 overflow-y-auto p-3">
          {llm && !ready ? (
            <div className="text-[11.5px] leading-relaxed text-muted-foreground">
              <b>Agent unavailable</b> — {llm.reason ?? 'no model configured'}.<br />
              Set a model in <b>Settings → Agent</b>.
              <div className="mt-2.5">
                <button data-testid="agent-configure" onClick={() => window.dispatchEvent(new CustomEvent('dp-open-settings'))}
                  className="inline-flex items-center gap-[5px] rounded-lg bg-[#6b4bd6] px-3 py-1.5 text-xs font-semibold text-white">
                  <Icon name="settings" size={12} /> Configure a model
                </button>
              </div>
            </div>
          ) : log.length === 0 && (
            <div className="text-[11.5px] leading-relaxed text-muted-foreground">
              Describe an outcome — e.g. <i>“sample images, filter where is_valid, write a table”</i>.
            </div>
          )}
          {log.map((m, i) => (
            <div key={i} className={cn('flex flex-col gap-1.5', m.role === 'user' ? 'items-end' : 'items-start')}>
              <div className={cn('max-w-[85%] whitespace-pre-wrap rounded-[10px] px-[11px] py-[7px] text-xs leading-snug text-foreground', m.role === 'user' ? 'bg-muted' : 'bg-[#6b4bd6]/10')}>
                {m.text}
              </div>
              {m.plan && (
                <div className="flex flex-wrap items-center gap-[5px]">
                  {m.plan.map((p, j) => (
                    <span key={j} className="inline-flex items-center gap-[5px] text-muted-foreground">
                      <span className="inline-flex items-center gap-[5px] rounded-[5px] bg-muted px-2 py-[3px] text-[10.5px] font-semibold text-muted-foreground">
                        <span className="h-1.5 w-1.5 rounded-full" style={{ background: kindAccent[stripTitle(p)] ?? color.text3 }} />
                        {p}
                      </span>
                      {j < m.plan!.length - 1 && <Icon name="chevronRight" size={11} />}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
          {busy && <div className="text-[11.5px] text-muted-foreground">working…</div>}
        </div>

        <div className="flex gap-2 border-t border-border p-2.5">
          <Input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
            placeholder={ready ? 'Describe an outcome…' : 'Configure a model to use the agent'}
            disabled={busy || !ready}
            className="flex-1 text-[12.5px] md:text-[12.5px]"
          />
          <button data-testid="agent-submit" onClick={submit} disabled={busy || !ready}
            className="rounded-md bg-[#6b4bd6] px-4 text-[12.5px] font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60">
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
