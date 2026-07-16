import { useEffect, useState } from 'react'
import { roleCanEdit, useStore } from '../store/graph'
import { api, toGraph, type AgentDataDisclosure } from '../api/client'
import { color, kindAccent } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/input'

// The agent is an actor (§5.8): each request carries only its own prompt and the current graph.
// The MODEL decides whether to answer/advise or to build by calling mutating tools
// (add/connect/set_config). We apply its graph to the canvas only when it did change it.
// Requires a configured model (DP_AGENT_MODEL + a provider key, set in Settings — the key stays in
// the kernel, never the browser). With no model it is simply unavailable — no rule-based stand-in.
// Before the first message, we show a data-egress disclosure from the workspace AgentDataPolicy.
export function AgentDock() {
  const open = useStore((s) => s.agentOpen)
  const setOpen = useStore((s) => s.setAgentOpen)
  const log = useStore((s) => s.agentLog)
  const doc = useStore((s) => s.doc)
  const push = useStore((s) => s.pushAgent)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [llm, setLlm] = useState<{
    available: boolean
    model?: string
    provider?: string
    reason?: string
    disclosure?: AgentDataDisclosure
  } | null>(null)

  useEffect(() => {
    if (!open) return
    if (!canEdit) { setOpen(false); return }
    api.agentStatus().then((s) => setLlm({
      available: s.available,
      model: s.model,
      provider: s.provider,
      reason: s.reason,
      disclosure: s.disclosure ?? s.policy,
    })).catch(() => setLlm({ available: false, reason: 'kernel offline' }))
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }  // Esc closes the dock
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [canEdit, open, setOpen])

  if (!open || !canEdit) return null

  const ready = !!llm?.available
  const disclosure = llm?.disclosure
  const graph = toGraph(doc)
  const completedRequests = groupRequests(log)
  const submit = async () => {
    if (!roleCanEdit(useStore.getState().canvasRole)) return
    const intent = text.trim()
    if (!intent || busy || !ready) return
    const requestDoc = useStore.getState().doc
    const requestCanvasId = requestDoc.id
    push({ role: 'user', text: intent })
    setText('')
    setBusy(true)
    try {
      const res = await api.agentAct(requestDoc, intent)
      if (useStore.getState().doc.id !== requestCanvasId) return
      if (res.available && res.graph) {
        const tx = res.transcript ?? []
        // the model chose to build iff it made a successful mutating tool call this turn; a pure
        // answer/plan turn (only reads, or text) leaves the canvas untouched.
        const mutated = tx.some((t) => ['add_node', 'connect', 'set_config'].includes(t.tool) && !t.result?.error)
        const built = tx.filter((t) => t.tool === 'add_node' && !t.result?.error).map((t) => String(t.input.kind))
        push({ role: 'agent', text: res.summary || (mutated ? 'Updated the canvas.' : 'Done.'), plan: built.length ? built : undefined })
        if (mutated && useStore.getState().applyAgentGraph(res.graph, requestCanvasId)) {
          runTerminal(res.graph)
        }
      } else {
        setLlm({ available: false, reason: res.reason, disclosure: res.disclosure ?? res.policy })
        push({ role: 'agent', text: `Agent unavailable — ${res.reason ?? 'no model configured'}. Set a model in Settings.` })
      }
    } catch (e) {
      if (useStore.getState().doc.id !== requestCanvasId) return
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
          <button onClick={() => setOpen(false)} className="grid h-[22px] w-6 place-items-center border-0 bg-transparent text-muted-foreground hover:text-foreground"><Icon name="close" size={13} /></button>
        </div>

        <div className="flex max-h-[260px] flex-col gap-3 overflow-y-auto p-3">
          {llm && !ready ? (
            <div className="text-[11.5px] leading-relaxed text-muted-foreground">
              <b>Agent unavailable</b> — {llm.reason ?? 'no model configured'}.<br />
              Set a model in <b>Settings → Agent</b>.
              <div className="mt-2.5">
                <button data-testid="agent-configure" onClick={(event) => window.dispatchEvent(new CustomEvent('dp-open-settings', { detail: event.currentTarget }))}
                  className="inline-flex items-center gap-[5px] rounded-lg bg-[#6b4bd6] px-3 py-1.5 text-xs font-semibold text-white">
                  <Icon name="settings" size={12} /> Configure a model
                </button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {ready && disclosure && (
                <div
                  data-testid="agent-egress-disclosure"
                  role="note"
                  className="rounded-md border border-border bg-muted/40 px-2.5 py-2 text-[11px] leading-relaxed text-muted-foreground"
                >
                  <div className="font-semibold text-foreground">This request is standalone</div>
                  <div className="mt-1" data-testid="agent-request-context">
                    Sends your prompt and the current graph: {graph.nodes.length} dataflow {graph.nodes.length === 1 ? 'node' : 'nodes'} and {graph.edges.length} {graph.edges.length === 1 ? 'connection' : 'connections'}.
                  </div>
                  <div className="mt-1">
                    Provider <span data-testid="agent-disclosure-provider">{disclosure.provider ?? llm?.provider ?? 'unknown'}</span>
                    {' · '}
                    model <span data-testid="agent-disclosure-model">{disclosure.model ?? llm?.model ?? 'unknown'}</span>
                  </div>
                  <div className="mt-1" data-testid="agent-disclosure-values">
                    {disclosure.rowValuesMayLeave
                      ? 'Sample row values may leave this deployment under the active AgentDataPolicy.'
                      : 'Sample row values will not leave this deployment (metadata-only). Catalog names and columns may still be sent to the model.'}
                  </div>
                  <div className="mt-1">Earlier requests and results shown here are display-only; they are not sent with this request.</div>
                </div>
              )}
              {completedRequests.length === 0 && <div className="text-[11.5px] leading-relaxed text-muted-foreground">
                Describe this request — e.g. <i>“sample images, filter where is_valid, write a table”</i>.
              </div>}
            </div>
          )}
          {completedRequests.length > 0 && <div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Completed requests — display only</div>}
          {completedRequests.map((request, i) => (
            <section key={i} className="flex flex-col gap-2 rounded-lg border border-border bg-muted/20 p-2.5" data-testid="agent-completed-request">
              <div>
                <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Request</div>
                <div className="whitespace-pre-wrap rounded-[10px] bg-muted px-[11px] py-[7px] text-xs leading-snug text-foreground">{request.intent}</div>
              </div>
              {request.result && <div>
                <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Result</div>
                <div className="whitespace-pre-wrap rounded-[10px] bg-[#6b4bd6]/10 px-[11px] py-[7px] text-xs leading-snug text-foreground">{request.result.text}</div>
                {request.result.plan && (
                  <div className="mt-1.5 flex flex-wrap items-center gap-[5px]">
                    {(request.result?.plan ?? []).map((p, j) => (
                      <span key={j} className="inline-flex items-center gap-[5px] text-muted-foreground">
                        <span className="inline-flex items-center gap-[5px] rounded-[5px] bg-muted px-2 py-[3px] text-[10.5px] font-semibold text-muted-foreground">
                          <span className="h-1.5 w-1.5 rounded-full" style={{ background: kindAccent[stripTitle(p)] ?? color.text3 }} />
                          {p}
                        </span>
                        {j < (request.result?.plan ?? []).length - 1 && <Icon name="chevronRight" size={11} />}
                      </span>
                    ))}
                  </div>
                )}
              </div>}
            </section>
          ))}
          {busy && <div className="text-[11.5px] text-muted-foreground">working…</div>}
        </div>

        <div className="flex gap-2 border-t border-border p-2.5">
          <Input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
            placeholder={ready ? 'Describe this request…' : 'Configure a model to use the agent'}
            disabled={busy || !ready}
            className="flex-1 text-[12.5px] md:text-[12.5px]"
          />
          <button data-testid="agent-submit" onClick={submit} disabled={busy || !ready}
            className="rounded-md bg-[#6b4bd6] px-4 text-[12.5px] font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60">
            Submit request
          </button>
        </div>
      </div>
    </div>
  )
}

function groupRequests(log: { role: 'user' | 'agent'; text: string; plan?: string[] }[]) {
  const requests: { intent: string; result?: { text: string; plan?: string[] } }[] = []
  for (const message of log) {
    if (message.role === 'user') {
      requests.push({ intent: message.text })
    } else if (requests.length) {
      requests[requests.length - 1].result = message
    }
  }
  return requests
}

// Run/preview the pipeline's terminal node so the agent "shows its work" after a build.
function runTerminal(bg: { nodes: { id: string; type: string }[]; edges: { source: string }[] }) {
  const sources = new Set(bg.edges.map((e) => e.source))
  const sink = [...bg.nodes].reverse().find((n) => !sources.has(n.id))
  if (!sink) return
  const store = useStore.getState()
  if (sink.type === 'write') store.requestRun(sink.id)
  else store.runPreview(sink.id)
}

function stripTitle(p: string): string {
  const known = ['source', 'sample', 'filter', 'select', 'transform', 'join', 'sql', 'aggregate', 'sort', 'dedup', 'write', 'metric', 'section']
  return known.find((k) => p.includes(k)) ?? 'transform'
}
