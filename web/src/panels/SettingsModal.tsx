import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { PluginInfo, ResourceSpec } from '../types/api'
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
  { id: 'execution', label: 'Execution', icon: 'db' },
  { id: 'destinations', label: 'Destinations', icon: 'export' },
  { id: 'plugins', label: 'Plugins', icon: 'grid' },
  { id: 'members', label: 'Members', icon: 'users' },
]

// sentinel for the runner select's "inherit the workspace default" option (Radix Select forbids an
// empty-string value); on save it maps back to '' so the per-user setting clears the override.
const INHERIT = '__default__'

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const kernelInfo = useStore((s) => s.kernelInfo)
  const users = useStore((s) => s.users)
  const currentUser = useStore((s) => s.currentUser)
  const authEnabled = useStore((s) => s.authEnabled)
  const refreshUsers = useStore((s) => s.refreshUsers)
  const pushToast = useStore((s) => s.pushToast)
  const canvasId = useStore((s) => s.doc.id)
  const [g, setG] = useState<Record<string, unknown>>({})
  const [u, setU] = useState<Record<string, unknown>>({})  // per-user settings (scope='user')
  const [loading, setLoading] = useState(true)
  const [savedMsg, setSavedMsg] = useState('')
  const [dest, setDest] = useState({ name: '', backend: 'local', root: '' })
  const [newUser, setNewUser] = useState({ name: '', password: '' })
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [pcfg, setPcfg] = useState<Record<string, Record<string, string>>>({})  // pack → edited { key: value }
  const [active, setActive] = useState('agent')
  const scrollRef = useRef<HTMLDivElement>(null)

  const addUser = async () => {
    const name = newUser.name.trim()
    if (!name) return
    try {
      await api.createUser(name, newUser.password || undefined)
      setNewUser({ name: '', password: '' })
      await refreshUsers()
      pushToast(`Added ${name}`, 'success')
    } catch (e) { pushToast((e as Error).message, 'error') }
  }

  useEffect(() => {
    api.getSettings().then((s) => { setG(s.global); setU(s.user) }).catch(() => {}).finally(() => setLoading(false))
    api.plugins().then(setPlugins).catch(() => {})
  }, [])

  // a plugin config field's currently-shown value: the user's edit, else the stored (non-secret) value
  const pval = (pack: string, key: string, stored: unknown) =>
    pcfg[pack]?.[key] ?? (stored == null ? '' : String(stored))
  const setPval = (pack: string, key: string, v: string) =>
    setPcfg((prev) => ({ ...prev, [pack]: { ...(prev[pack] ?? {}), [key]: v } }))
  const configurable = plugins.filter((p) => (p.config?.length ?? 0) > 0)

  const val = (k: string) => (g[k] == null ? '' : String(g[k]))
  const set = (k: string, v: string) => setG((prev) => ({ ...prev, [k]: v }))
  const dests = (Array.isArray(g.destinations) ? g.destinations : []) as { id: string; name: string; backend: string; root: string }[]
  const obj = (g.objectStore && typeof g.objectStore === 'object' ? g.objectStore : {}) as Record<string, string>
  const setObj = (k: string, v: string) => setG((prev) => ({ ...prev, objectStore: { ...(prev.objectStore as object ?? {}), [k]: v } }))
  const save = async () => {
    // only admins may write global settings (auth mode); default true keeps single-user + older
    // backends unchanged. Skipping the global writes a non-admin can't do avoids doomed 403s for
    // settings they never touched — their own per-user runner still saves.
    const canGlobal = currentUser?.capabilities?.includes('global_settings') ?? true
    try {
      if (canGlobal) {
        for (const k of ['agentModel', 'agentApiKey', 'agentBaseUrl']) {
          await api.putSetting('global', k, g[k] ?? '')
        }
        await api.putSetting('global', 'destinations', dests)
        await api.putSetting('global', 'objectStore', obj)
        // edited plugin config → plugin.<pack>.<key> (skip a blank secret so it keeps its existing value)
        for (const [pack, fields] of Object.entries(pcfg)) {
          const schema = plugins.find((p) => p.name === pack)?.config ?? []
          for (const [key, v] of Object.entries(fields)) {
            if (schema.find((f) => f.key === key)?.secret && !v) continue
            await api.putSetting('global', `plugin.${pack}.${key}`, v)
          }
        }
      }
      // the runner is a per-user preference (everyone may set it); the sentinel = "inherit the default"
      await api.putSetting('user', 'backend', u.backend === INHERIT ? '' : (u.backend ?? ''))
      setSavedMsg('Saved'); setTimeout(() => setSavedMsg(''), 1400)
    } catch (e) {
      pushToast((e as Error).message, 'error')  // a real failure is surfaced, never a false "Saved"
    }
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
                    <Select value={(u.backend ? String(u.backend) : INHERIT)} onValueChange={(v) => setU((p) => ({ ...p, backend: v }))}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value={INHERIT}>Workspace default{g.backend ? ` (${String(g.backend)})` : ` (${runners[0]})`}</SelectItem>
                        {runners.map((r) => <SelectItem key={r} value={r}>{r}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </Field>
                  <div className="-mt-1 text-[10.5px] text-muted-foreground">Your preference for your own runs — falls back to the workspace default.</div>
                  {((u.backend && u.backend !== INHERIT ? String(u.backend) : (g.backend ? String(g.backend) : runners[0])) === 'kernel') && (
                    <div className="mt-2 flex items-center gap-2">
                      <Button variant="outline" size="sm" onClick={async () => {
                        try {
                          const r = await api.restartKernel(canvasId)
                          pushToast(r.restarted ? 'Kernel restarting…' : 'No live kernel — a fresh one starts on the next run', 'success')
                        } catch (e) { pushToast((e as Error).message, 'error') }  // a failed restart is an error, not success
                      }}>Restart kernel</Button>
                      <span className="text-[10.5px] text-muted-foreground">Clears this canvas's warm kernel (a wedged transform / stale state); the next run starts fresh.</span>
                    </div>
                  )}

                  <div className="mb-1.5 mt-4 text-[11.5px] font-semibold text-foreground">Compute</div>
                  <div className="mb-2 text-[10.5px] text-muted-foreground">Backends and the workers (pods / processes) they offer, with capacity. A pod/Ray backend plugin adds its own here.</div>
                  <div className="flex flex-col gap-1.5">
                    {(kernelInfo?.backends ?? []).map((b) => (
                      <div key={b.name} className="rounded-md border border-border p-2">
                        <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
                          <Icon name="db" size={12} /> {b.name}
                          <span className="text-[10px] font-normal text-muted-foreground">· {b.workers.length} worker{b.workers.length === 1 ? '' : 's'}</span>
                        </div>
                        <div className="mt-1 flex flex-col gap-0.5">
                          {b.workers.map((w) => (
                            <div key={w.id} className="flex items-center gap-1.5 text-[10.5px] text-muted-foreground">
                              <span className={cn('h-1.5 w-1.5 rounded-full', w.state === 'idle' ? 'bg-green-500' : w.state === 'busy' ? 'bg-amber-500' : 'bg-muted-foreground')} />
                              <span className="font-mono">{w.id}</span><span>· {capLabel(w.capacity)}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                    {(kernelInfo?.backends ?? []).length === 0 && <div className="text-[11.5px] text-muted-foreground">No backends reported.</div>}
                  </div>
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

                <Section id="plugins" title="Plugins">
                  <p className="mb-2 text-[11.5px] leading-relaxed text-muted-foreground">
                    Loaded plugin packs. A pack that declares config fields (in its <code>dataplay.toml</code>) can be set here.
                    Changes take effect on the next kernel start.
                  </p>
                  <div className="mb-2.5 flex flex-col gap-1">
                    {plugins.map((p) => (
                      <div key={p.name} className="flex items-center gap-2 text-xs text-foreground">
                        <span className="flex items-center text-muted-foreground"><Icon name={p.error ? 'close' : 'check'} size={12} /></span>
                        <span className="font-semibold">{p.name}</span>
                        {p.version && <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">{p.version}</Badge>}
                        <span className="text-[10px] text-muted-foreground">{p.source}</span>
                        {p.error && <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[10.5px] text-destructive">{p.error}</span>}
                      </div>
                    ))}
                    {plugins.length === 0 && <div className="text-[11.5px] text-muted-foreground">No plugins loaded.</div>}
                  </div>

                  {configurable.map((p) => (
                    <div key={p.name} className="mt-3 rounded-md border border-border p-3">
                      <div className="mb-2 flex items-center gap-1.5 text-[12px] font-semibold text-foreground">
                        <Icon name="settings" size={12} /> {p.name}
                      </div>
                      {p.config!.map((f) => {
                        const isSet = p.config_set?.includes(f.key)
                        const ph = f.placeholder ?? (f.secret ? (isSet ? '•••••• (set — blank keeps it)' : 'not set')
                          : (f.default != null ? String(f.default) : ''))
                        return (
                          <Field key={f.key} label={f.label}>
                            {f.type === 'select' && f.options ? (
                              <Select value={pval(p.name, f.key, p.config_values?.[f.key])} onValueChange={(v) => setPval(p.name, f.key, v)}>
                                <SelectTrigger><SelectValue placeholder={ph} /></SelectTrigger>
                                <SelectContent>{f.options.map((o) => <SelectItem key={o} value={o}>{o}</SelectItem>)}</SelectContent>
                              </Select>
                            ) : f.type === 'bool' ? (
                              <Select value={pval(p.name, f.key, p.config_values?.[f.key]) || 'false'} onValueChange={(v) => setPval(p.name, f.key, v)}>
                                <SelectTrigger><SelectValue /></SelectTrigger>
                                <SelectContent><SelectItem value="true">true</SelectItem><SelectItem value="false">false</SelectItem></SelectContent>
                              </Select>
                            ) : (
                              <Input
                                type={f.secret || f.type === 'password' ? 'password' : (f.type === 'int' || f.type === 'float' ? 'number' : 'text')}
                                value={f.secret ? (pcfg[p.name]?.[f.key] ?? '') : pval(p.name, f.key, p.config_values?.[f.key])}
                                placeholder={ph}
                                onChange={(e) => setPval(p.name, f.key, e.target.value)}
                              />
                            )}
                            {f.help && <div className="mt-1 text-[10.5px] text-muted-foreground">{f.help}</div>}
                          </Field>
                        )
                      })}
                    </div>
                  ))}
                  {configurable.length === 0 && <div className="text-[11.5px] text-muted-foreground">No plugin declares configurable settings.</div>}
                </Section>

                <Section id="members" title="Members">
                  <p className="mb-2 text-[11.5px] leading-relaxed text-muted-foreground">
                    People who can sign in and be added as collaborators.
                    {authEnabled
                      ? ' Set an initial password below; each member can then rotate their own from the account menu.'
                      : ' Sign-in is off (no DP_AUTH_SECRET), so passwords are unused until you enable auth — anyone with the URL is trusted.'}
                  </p>
                  <div className="mb-2.5 flex flex-col gap-1">
                    {users.map((usr) => (
                      <div key={usr.id} className="flex items-center gap-2 text-xs text-foreground">
                        <span className="grid h-[22px] w-[22px] place-items-center rounded-full bg-muted text-[10px] font-bold text-muted-foreground">{usr.name.slice(0, 1).toUpperCase()}</span>
                        <span className="flex-1">{usr.name}</span>
                        {usr.id === currentUser?.id && <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">you</Badge>}
                      </div>
                    ))}
                  </div>
                  <div className="flex gap-1.5">
                    <Input value={newUser.name} onChange={(e) => setNewUser({ ...newUser, name: e.target.value })} placeholder="Name" className="w-[150px] shrink-0" />
                    <Input type="password" value={newUser.password} onChange={(e) => setNewUser({ ...newUser, password: e.target.value })}
                      onKeyDown={(e) => { if (e.key === 'Enter') addUser() }}
                      placeholder={authEnabled ? 'Initial password (optional)' : 'Password (unused until auth is on)'} className="min-w-0 flex-1" />
                    <Button onClick={addUser} disabled={!newUser.name.trim()} className="shrink-0">Add member</Button>
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

function capLabel(c: ResourceSpec): string {
  const parts: string[] = []
  if (c.gpu) parts.push(`${c.gpu}× ${c.gpuType ?? 'gpu'}`)
  if (c.cpu) parts.push(`${c.cpu} cpu`)
  if (c.mem) parts.push(String(c.mem))
  return parts.join(' · ') || 'unspecified'
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-2.5">
      <Label className="mb-1 block text-[11.5px] font-normal text-muted-foreground">{label}</Label>
      {children}
    </div>
  )
}
