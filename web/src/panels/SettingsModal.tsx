import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { useStore } from '../store/graph'
import { Icon, type IconName } from '../ui/Icon'
import { cn } from '@/lib/utils'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

// App / workspace settings — a full-screen page with a left category nav (like Figma / most apps),
// not a cramped modal. These are GLOBAL: the LLM agent (provider-agnostic; the key lives in the
// kernel), the execution backend, and save/open destinations. Datasets have their own Tables page;
// canvas-scoped settings live in the separate CanvasSettingsModal (opened from the file menu).
const CATS: { id: string; label: string; icon: IconName }[] = [
  { id: 'agent', label: 'Agent', icon: 'sparkle' },
  { id: 'execution', label: 'Execution', icon: 'play' },
  { id: 'destinations', label: 'Destinations', icon: 'export' },
]

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const kernelInfo = useStore((s) => s.kernelInfo)
  const [g, setG] = useState<Record<string, unknown>>({})
  const [loading, setLoading] = useState(true)
  const [savedMsg, setSavedMsg] = useState('')
  const [dest, setDest] = useState({ name: '', backend: 'local', root: '' })
  const [active, setActive] = useState('agent')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api.getSettings().then((s) => setG(s.global)).catch(() => {}).finally(() => setLoading(false))
  }, [])

  const val = (k: string) => (g[k] == null ? '' : String(g[k]))
  const set = (k: string, v: string) => setG((prev) => ({ ...prev, [k]: v }))
  const dests = (Array.isArray(g.destinations) ? g.destinations : []) as { id: string; name: string; backend: string; root: string }[]
  const obj = (g.objectStore && typeof g.objectStore === 'object' ? g.objectStore : {}) as Record<string, string>
  const setObj = (k: string, v: string) => setG((prev) => ({ ...prev, objectStore: { ...(prev.objectStore as object ?? {}), [k]: v } }))
  const save = async () => {
    for (const k of ['agentModel', 'agentApiKey', 'agentBaseUrl', 'backend']) {
      await api.putSetting('global', k, g[k] ?? '').catch(() => {})
    }
    await api.putSetting('global', 'destinations', dests).catch(() => {})
    await api.putSetting('global', 'objectStore', obj).catch(() => {})
    setSavedMsg('Saved'); setTimeout(() => setSavedMsg(''), 1400)
  }
  const addDest = () => {
    const name = dest.name.trim(), root = dest.root.trim()
    if (!name || !root) return
    const id = `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${Math.abs(Math.floor(Math.random() * 1e6))}`
    setG((prev) => ({ ...prev, destinations: [...dests, { id, name, backend: dest.backend, root }] }))
    setDest({ name: '', backend: 'local', root: '' })
  }
  const go = (id: string) => { setActive(id); document.getElementById(`set-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
  const runners = kernelInfo?.runners ?? ['local-out-of-core']

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="dp-modal-overlay flex flex-col gap-0 overflow-hidden p-0 w-[94vw] max-w-[940px] h-[min(680px,90vh)]">
        {/* header */}
        <div className="flex items-center gap-2 border-b border-border py-3 pl-[18px] pr-12">
          <span className="flex items-center text-muted-foreground"><Icon name="settings" size={15} /></span>
          <DialogTitle className="text-[15px] font-bold">Settings</DialogTitle>
          <span className="flex-1" />
          <span className="text-[11.5px] text-green-600">{savedMsg}</span>
          <Button size="sm" onClick={save}>Save</Button>
        </div>
        <DialogDescription className="sr-only">Application and workspace settings: the agent model, execution backend, and output destinations.</DialogDescription>

        <div className="flex min-h-0 flex-1">
          {/* left category nav */}
          <nav className="flex w-[190px] shrink-0 flex-col gap-0.5 border-r border-border p-3">
            {CATS.map((c) => (
              <button key={c.id} onClick={() => go(c.id)}
                className={cn('flex items-center gap-[9px] rounded-md px-2.5 py-2 text-left text-[12.5px] font-medium transition-colors',
                  active === c.id ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent/50')}>
                <Icon name={c.icon} size={14} /> {c.label}
              </button>
            ))}
          </nav>

          {/* content — all sections rendered; the nav scroll-jumps to them */}
          <div ref={scrollRef} className="min-w-0 flex-1 overflow-y-auto px-[22px] py-[18px]">
            {loading ? <div className="text-[12.5px] text-muted-foreground">loading…</div> : (
              <div className="flex flex-col gap-[26px]">
                <Section id="agent" title="Agent (LLM)">
                  <Field label="Model"><Input value={val('agentModel')} placeholder="anthropic/claude-opus-4-8" onChange={(e) => set('agentModel', e.target.value)} /></Field>
                  <div className="-mt-1 mb-2 text-[10.5px] text-muted-foreground">e.g. anthropic/claude-opus-4-8 · openai/gpt-5 · google/gemini-2.5-pro · ollama/llama3.3</div>
                  <Field label="API key"><Input type="password" value={val('agentApiKey')} placeholder="sk-… (or blank to use an env var)" onChange={(e) => set('agentApiKey', e.target.value)} /></Field>
                  <Field label="Base URL"><Input value={val('agentBaseUrl')} placeholder="http://localhost:11434 (optional)" onChange={(e) => set('agentBaseUrl', e.target.value)} /></Field>
                </Section>

                <Section id="execution" title="Execution backend">
                  <Field label="Runner">
                    <Select value={val('backend') || runners[0]} onValueChange={(v) => set('backend', v)}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {runners.map((r) => <SelectItem key={r} value={r}>{r}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </Field>
                </Section>

                <Section id="destinations" title="Destinations">
                  <p className="mb-2 text-[11.5px] leading-relaxed text-muted-foreground">
                    Named places to save outputs / open files: a local directory, or an object-store prefix (s3://, gs://).
                  </p>
                  <div className="mb-2 flex flex-col gap-1">
                    {dests.map((d, i) => (
                      <div key={d.id} className="flex items-center gap-2 text-xs text-foreground">
                        <span className="flex items-center text-muted-foreground"><Icon name="export" size={12} /></span>
                        <span className="font-semibold">{d.name}</span>
                        <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">{d.backend}</Badge>
                        <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[11px] text-muted-foreground">{d.root}</span>
                        <button onClick={() => setG((prev) => ({ ...prev, destinations: dests.filter((_, j) => j !== i) }))}
                          className="grid place-items-center text-muted-foreground transition-colors hover:text-foreground"><Icon name="close" size={12} /></button>
                      </div>
                    ))}
                    {dests.length === 0 && <div className="text-[11.5px] text-muted-foreground">Only the default "Workspace outputs".</div>}
                  </div>
                  <div className="flex gap-1.5">
                    <Input value={dest.name} onChange={(e) => setDest({ ...dest, name: e.target.value })} placeholder="e.g. S3 exports" className="w-[120px] shrink-0" />
                    <Select value={dest.backend} onValueChange={(v) => setDest({ ...dest, backend: v })}>
                      <SelectTrigger className="w-[84px] shrink-0"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="local">local</SelectItem>
                        <SelectItem value="s3">s3</SelectItem>
                        <SelectItem value="gs">gs</SelectItem>
                      </SelectContent>
                    </Select>
                    <Input value={dest.root} onChange={(e) => setDest({ ...dest, root: e.target.value })} onKeyDown={(e) => { if (e.key === 'Enter') addDest() }}
                      placeholder={dest.backend === 'local' ? '/path/to/dir' : `${dest.backend}://bucket/prefix`}
                      className="min-w-0 flex-1" />
                    <Button onClick={addDest} className="shrink-0">Add</Button>
                  </div>

                  <div className="mb-1.5 mt-4 text-[11.5px] font-semibold text-foreground">Object-store credentials</div>
                  <div className="mb-2 text-[10.5px] text-muted-foreground">Leave blank to use the environment (AWS_* / ~/.aws / instance role). Set keys for MinIO / R2 / other S3-compatible endpoints.</div>
                  <div className="grid grid-cols-2 gap-1.5">
                    <Input value={obj.accessKeyId ?? ''} placeholder="access key id" onChange={(e) => setObj('accessKeyId', e.target.value)} />
                    <Input type="password" value={obj.secretAccessKey ?? ''} placeholder="secret access key" onChange={(e) => setObj('secretAccessKey', e.target.value)} />
                    <Input value={obj.region ?? ''} placeholder="region (e.g. us-east-1)" onChange={(e) => setObj('region', e.target.value)} />
                    <Input value={obj.endpoint ?? ''} placeholder="endpoint (MinIO/R2, optional)" onChange={(e) => setObj('endpoint', e.target.value)} />
                  </div>
                </Section>
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <div id={`set-${id}`} className="scroll-mt-2">
      <div className="mb-3 text-[13px] font-bold text-foreground">{title}</div>
      {children}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-2.5">
      <Label className="mb-1 block text-[11.5px] font-normal text-muted-foreground">{label}</Label>
      {children}
    </div>
  )
}
