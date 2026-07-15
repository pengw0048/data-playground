import { useEffect, useState } from 'react'
import { api, type Cred, type CredKind } from '../api/client'
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
  { id: 'credentials', label: 'Credentials', icon: 'link' },
  { id: 'plugins', label: 'Plugins', icon: 'grid' },
  { id: 'members', label: 'Members', icon: 'users' },
]

// sentinel for the runner select's "inherit the workspace default" option (Radix Select forbids an
// empty-string value); on save it maps back to '' so the per-user setting clears the override.
const INHERIT = '__default__'
// Radix Select forbids an empty value — sentinels for "no credential" pickers (mapped to '' on save).
const NO_CRED = '__none__'
const OBJECT_STORE_FIELDS: { key: string; placeholder: string }[] = [
  { key: 'accessKeyId', placeholder: 'env:AWS_ACCESS_KEY_ID' },
  { key: 'secretAccessKey', placeholder: 'env:AWS_SECRET_ACCESS_KEY' },
  { key: 'region', placeholder: 'region (e.g. us-east-1)' },
  { key: 'endpoint', placeholder: 'endpoint (MinIO/R2, optional)' },
]
type CredForm = { id: string | null; name: string; kind: CredKind; fields: Record<string, string> }
const emptyCredForm = (kind: CredKind): CredForm => ({ id: null, name: '', kind, fields: {} })

type SaveFailure = {
  message: string
  completed: number
  total: number
}

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)

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
  const [loadError, setLoadError] = useState('')
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [saving, setSaving] = useState(false)
  const [saveFailure, setSaveFailure] = useState<SaveFailure | null>(null)
  const [savedMsg, setSavedMsg] = useState('')
  const [dest, setDest] = useState<{ name: string; backend: string; root: string; credId: string }>({ name: '', backend: 'local', root: '', credId: NO_CRED })
  const [creds, setCreds] = useState<Cred[]>([])
  const [credForm, setCredForm] = useState<CredForm>(emptyCredForm('object_store'))
  const [newUser, setNewUser] = useState({ name: '', password: '' })
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [pcfg, setPcfg] = useState<Record<string, Record<string, string>>>({})  // pack → edited { key: value }
  const [active, setActive] = useState('agent')
  // /api/me is authoritative. Missing capabilities must fail closed: open/single-user mode also
  // receives global_settings, so there is no need for a permissive fallback while identity loads.
  const canGlobal = currentUser?.capabilities?.includes('global_settings') === true
  const categories = canGlobal ? CATS : CATS.filter((c) => c.id === 'execution')

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
    let cancelled = false
    setLoading(true)
    setLoadError('')
    const settings = api.getSettings().catch((error) => {
      throw new Error(`Settings request failed: ${errorMessage(error)}`)
    })
    const pluginPacks = canGlobal
      ? api.plugins().catch((error) => { throw new Error(`Plugins request failed: ${errorMessage(error)}`) })
      : Promise.resolve([] as PluginInfo[])
    const credList = canGlobal
      ? api.listCreds().catch((error) => { throw new Error(`Credentials request failed: ${errorMessage(error)}`) })
      : Promise.resolve([] as Cred[])
    Promise.all([settings, pluginPacks, credList]).then(([nextSettings, nextPlugins, nextCreds]) => {
      if (cancelled) return
      const global = { ...nextSettings.global }
      const policy = (global.agentDataPolicy && typeof global.agentDataPolicy === 'object')
        ? global.agentDataPolicy as { level?: string; endpointIsLocal?: boolean }
        : null
      global.agentDataPolicyLevel = policy?.level || 'metadata-only'
      global.agentDataPolicyEndpointIsLocal = Boolean(policy?.endpointIsLocal)
      setG(global)
      setU(nextSettings.user)
      setPlugins(nextPlugins)
      setCreds(nextCreds)
      setLoading(false)
    }).catch((error) => {
      if (cancelled) return
      setLoadError(errorMessage(error))
      setLoading(false)
    })
    return () => { cancelled = true }
  }, [canGlobal, loadAttempt])

  useEffect(() => {
    if (!canGlobal && active !== 'execution') setActive('execution')
  }, [active, canGlobal])

  // a plugin config field's currently-shown value: the user's edit, else the stored (non-secret) value
  const pval = (pack: string, key: string, stored: unknown) =>
    pcfg[pack]?.[key] ?? (stored == null ? '' : String(stored))
  const setPval = (pack: string, key: string, v: string) =>
    setPcfg((prev) => ({ ...prev, [pack]: { ...(prev[pack] ?? {}), [key]: v } }))
  const configurable = plugins.filter((p) => (p.config?.length ?? 0) > 0)

  const val = (k: string) => (g[k] == null ? '' : String(g[k]))
  const set = (k: string, v: string) => setG((prev) => ({ ...prev, [k]: v }))
  const dests = (Array.isArray(g.destinations) ? g.destinations : []) as { id: string; name: string; backend: string; root: string; credId?: string | null }[]
  const objectStoreCreds = creds.filter((c) => c.kind === 'object_store')
  const agentCreds = creds.filter((c) => c.kind === 'agent')
  const credName = (id?: string | null) => creds.find((c) => c.id === id)?.name
  const save = async () => {
    if (loading || loadError || saving) return
    const updates: { scope: 'global' | 'user'; key: string; value: unknown }[] = []
    // Only admins may write global settings. Non-admins see just their per-user runner preference,
    // so the request list mirrors exactly what the UI says they can change.
    if (canGlobal) {
      for (const key of ['agentModel', 'agentBaseUrl']) {
        updates.push({ scope: 'global', key, value: g[key] ?? '' })
      }
      // The agent's key and object-store credentials are Cred entities now (managed in the Credentials
      // pane); settings only reference them by id.
      updates.push({ scope: 'global', key: 'agentCredId', value: g.agentCredId === NO_CRED ? '' : (g.agentCredId ?? '') })
      updates.push({ scope: 'global', key: 'defaultObjectStoreCredId', value: g.defaultObjectStoreCredId === NO_CRED ? '' : (g.defaultObjectStoreCredId ?? '') })
      updates.push({
        scope: 'global',
        key: 'agentDataPolicy',
        value: {
          level: String(g.agentDataPolicyLevel || 'metadata-only'),
          endpointIsLocal: Boolean(g.agentDataPolicyEndpointIsLocal),
        },
      })
      updates.push({ scope: 'global', key: 'destinations', value: dests })
      for (const [pack, fields] of Object.entries(pcfg)) {
        const schema = plugins.find((p) => p.name === pack)?.config ?? []
        for (const [key, value] of Object.entries(fields)) {
          if (schema.find((f) => f.key === key)?.secret && !value) continue
          updates.push({ scope: 'global', key: `plugin.${pack}.${key}`, value })
        }
      }
    }
    updates.push({ scope: 'user', key: 'backend', value: u.backend === INHERIT ? '' : (u.backend ?? '') })

    setSaving(true)
    setSavedMsg('')
    setSaveFailure(null)
    let completed = 0
    try {
      for (const update of updates) {
        await api.putSetting(update.scope, update.key, update.value)
        completed += 1
      }
      setSavedMsg('Saved'); setTimeout(() => setSavedMsg(''), 1400)
    } catch (e) {
      const failed = updates[completed]
      const target = failed ? `${failed.scope} setting "${failed.key}"` : 'settings'
      const message = `Could not save ${target}: ${errorMessage(e)}`
      setSaveFailure({ message, completed, total: updates.length })
      pushToast(message, 'error')
    } finally {
      setSaving(false)
    }
  }
  const addDest = () => {
    const name = dest.name.trim(), root = dest.root.trim()
    if (!name || !root) return
    const id = `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${Math.abs(Math.floor(Math.random() * 1e6))}`
    const credId = dest.backend !== 'local' && dest.credId !== NO_CRED ? dest.credId : null
    setG((prev) => ({ ...prev, destinations: [...dests, { id, name, backend: dest.backend, root, credId }] }))
    setDest({ name: '', backend: 'local', root: '', credId: NO_CRED })
  }
  const setCredField = (k: string, v: string) => setCredForm((p) => ({ ...p, fields: { ...p.fields, [k]: v } }))
  const editCred = (c: Cred) => setCredForm({ id: c.id, name: c.name, kind: c.kind, fields: { ...c.fields } })
  const saveCred = async () => {
    const name = credForm.name.trim()
    if (!name) return
    // Send only non-empty reference fields; a blank field is omitted (keeps refs, never writes plaintext).
    const fields = Object.fromEntries(Object.entries(credForm.fields).filter(([, v]) => v.trim() !== ''))
    try {
      const body = { name, kind: credForm.kind, fields }
      const saved = credForm.id ? await api.updateCred(credForm.id, body) : await api.createCred(body)
      setCreds((prev) => credForm.id ? prev.map((c) => c.id === saved.id ? saved : c) : [...prev, saved])
      setCredForm(emptyCredForm(credForm.kind))
      pushToast(`Saved credential ${name}`, 'success')
    } catch (e) { pushToast((e as Error).message, 'error') }
  }
  const removeCred = async (c: Cred) => {
    try {
      await api.deleteCred(c.id)
      setCreds((prev) => prev.filter((x) => x.id !== c.id))
      if (g.defaultObjectStoreCredId === c.id) setG((prev) => ({ ...prev, defaultObjectStoreCredId: '' }))
      if (g.agentCredId === c.id) setG((prev) => ({ ...prev, agentCredId: '' }))
      pushToast(`Removed credential ${c.name}`, 'success')
    } catch (e) { pushToast((e as Error).message, 'error') }
  }
  const go = (id: string) => setActive(id)  // master-detail: the nav switches the visible pane
  const runners = kernelInfo?.runners ?? ['local-out-of-core']

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent data-testid="settings-modal" className="dp-modal-overlay flex flex-col gap-0 overflow-hidden p-0 w-[94vw] max-w-[940px] h-[min(680px,90vh)]">
        {/* header */}
        <div className="flex items-center gap-2 border-b border-border py-3 pl-[18px] pr-12">
          <span className="flex items-center text-muted-foreground"><Icon name="settings" size={15} /></span>
          <DialogTitle className="text-[15px] font-bold">Settings</DialogTitle>
          <span className="flex-1" />
          <span className="text-[11.5px] text-green-600">{savedMsg}</span>
          <Button size="sm" onClick={save} disabled={loading || Boolean(loadError) || saving}>{saving ? 'Saving…' : 'Save'}</Button>
        </div>
        <DialogDescription className="sr-only">Application and workspace settings: the agent model, execution backend, and output destinations.</DialogDescription>

        {saveFailure && (
          <div role="alert" className="flex items-center gap-3 border-b border-destructive/30 bg-destructive/5 px-[18px] py-2 text-[11.5px] text-destructive">
            <div className="min-w-0 flex-1">
              <div>{saveFailure.message}</div>
              <div className="mt-0.5 text-[10.5px]">
                {saveFailure.completed > 0
                  ? `${saveFailure.completed} of ${saveFailure.total} updates completed before the failure. Server settings may be partially updated; your edits remain here.`
                  : 'No update was confirmed. Your edits remain here.'}
              </div>
            </div>
            <Button variant="outline" size="sm" onClick={save} disabled={saving}>Retry save</Button>
          </div>
        )}

        <div className="flex min-h-0 flex-1">
          {/* left category nav */}
          <nav className="flex w-[190px] shrink-0 flex-col gap-0.5 border-r border-border p-3">
            {categories.map((c) => (
              <button key={c.id} onClick={() => go(c.id)}
                className={cn('flex items-center gap-[9px] rounded-md px-2.5 py-2 text-left text-[12.5px] font-medium transition-colors',
                  active === c.id ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent/50')}>
                <Icon name={c.icon} size={14} /> {c.label}
              </button>
            ))}
          </nav>

          {/* content — only the active pane renders (master-detail); the nav switches panes */}
          <div className="min-w-0 flex-1 overflow-y-auto px-[22px] py-[18px]">
            {loading ? <div className="text-[12.5px] text-muted-foreground">loading…</div> : loadError ? (
              <div role="alert" className="mx-auto flex h-full max-w-[440px] flex-col items-center justify-center text-center">
                <div className="text-[13px] font-semibold text-foreground">Settings could not be loaded</div>
                <div className="mt-1.5 text-[11.5px] leading-relaxed text-destructive">{loadError}</div>
                <div className="mt-2 text-[10.5px] leading-relaxed text-muted-foreground">The editor is blocked so unavailable data is never replaced with empty defaults.</div>
                <Button variant="outline" size="sm" className="mt-4" onClick={() => setLoadAttempt((n) => n + 1)}>Retry loading</Button>
              </div>
            ) : (
              <div className="flex flex-col gap-[26px]">
                {canGlobal && active === 'agent' && <Section id="agent" title="Agent (LLM)">
                  <Field label="Model"><Input value={val('agentModel')} placeholder="anthropic/claude-opus-4-8" onChange={(e) => set('agentModel', e.target.value)} /></Field>
                  <div className="-mt-1 mb-2 text-[10.5px] text-muted-foreground">e.g. anthropic/claude-opus-4-8 · openai/gpt-5 · google/gemini-2.5-pro · ollama/llama3.3</div>
                  <Field label="API key credential">
                    <Select value={g.agentCredId ? String(g.agentCredId) : NO_CRED} onValueChange={(v) => set('agentCredId', v === NO_CRED ? '' : v)}>
                      <SelectTrigger aria-label="Agent credential"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value={NO_CRED}>None (use the provider&apos;s env var)</SelectItem>
                        {agentCreds.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </Field>
                  <div className="-mt-1 mb-2 text-[10.5px] text-muted-foreground">Pick an <span className="font-medium">agent</span> credential (managed in the Credentials pane). Its key is a reference (`env:VAR` / `file:/path`), never stored raw.</div>
                  <Field label="Base URL"><Input value={val('agentBaseUrl')} placeholder="http://localhost:11434 (optional)" onChange={(e) => set('agentBaseUrl', e.target.value)} /></Field>
                  <Field label="Data policy">
                    <Select
                      value={String(g.agentDataPolicyLevel || 'metadata-only')}
                      onValueChange={(v) => setG((prev) => ({ ...prev, agentDataPolicyLevel: v }))}
                    >
                      <SelectTrigger aria-label="Data policy"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="metadata-only">metadata-only (default for hosted models)</SelectItem>
                        <SelectItem value="sample-values">sample-values (send up to 8 preview rows)</SelectItem>
                      </SelectContent>
                    </Select>
                  </Field>
                  <div className="-mt-1 mb-2 text-[10.5px] text-muted-foreground">
                    Hosted providers default to metadata-only so catalog identity may leave but sample cell values do not.
                    Opt into sample-values only when that third-party egress is acceptable.
                  </div>
                  <label className="mb-2 flex items-start gap-2 text-[11.5px] text-foreground">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={Boolean(g.agentDataPolicyEndpointIsLocal)}
                      onChange={(e) => setG((prev) => ({ ...prev, agentDataPolicyEndpointIsLocal: e.target.checked }))}
                    />
                    <span>
                      Treat Base URL as a local / self-hosted endpoint
                      <span className="mt-0.5 block text-[10.5px] text-muted-foreground">
                        When set, sample values may reach that endpoint without the sample-values opt-in.
                        Does nothing unless a Base URL is configured.
                      </span>
                    </span>
                  </label>
                </Section>}

                {active === 'execution' && <Section id="execution" title="Execution backend">
                  {!canGlobal && <div className="mb-3 rounded-md border border-border bg-muted/40 p-2.5 text-[10.5px] text-muted-foreground">Workspace-wide settings are managed by an administrator. You can still change your runner preference.</div>}
                  <Field label="Runner">
                    <Select value={(u.backend ? String(u.backend) : INHERIT)} onValueChange={(v) => setU((p) => ({ ...p, backend: v }))}>
                      <SelectTrigger aria-label="Runner"><SelectValue /></SelectTrigger>
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
                </Section>}

                {canGlobal && active === 'destinations' && <Section id="destinations" title="Destinations">
                  <p className="mb-2 text-[11.5px] leading-relaxed text-muted-foreground">
                    Named places to save outputs / open files: a local directory, or an object-store prefix (s3://, gs://).
                  </p>
                  <div className="mb-2 flex flex-col gap-1">
                    {dests.map((d, i) => (
                      <div key={d.id} className="flex items-center gap-2 text-xs text-foreground">
                        <span className="flex items-center text-muted-foreground"><Icon name="export" size={12} /></span>
                        <span className="font-semibold">{d.name}</span>
                        <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">{d.backend}</Badge>
                        {d.credId && <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">{credName(d.credId) ?? 'credential'}</Badge>}
                        <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[11px] text-muted-foreground">{d.root}</span>
                        <button onClick={() => setG((prev) => ({ ...prev, destinations: dests.filter((_, j) => j !== i) }))}
                          aria-label={`Remove destination ${d.name}`}
                          className="grid place-items-center text-muted-foreground transition-colors hover:text-foreground"><Icon name="close" size={12} /></button>
                      </div>
                    ))}
                    {dests.length === 0 && <div className="text-[11.5px] text-muted-foreground">Only the default "Workspace outputs".</div>}
                  </div>
                  <div className="flex gap-1.5">
                    <Input value={dest.name} onChange={(e) => setDest({ ...dest, name: e.target.value })} placeholder="e.g. S3 exports" className="w-[120px] shrink-0" aria-label="Destination name" />
                    <Select value={dest.backend} onValueChange={(v) => setDest({ ...dest, backend: v, credId: v === 'local' ? NO_CRED : dest.credId })}>
                      <SelectTrigger className="w-[84px] shrink-0" aria-label="Destination backend"><SelectValue /></SelectTrigger>
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
                  {dest.backend !== 'local' && (
                    <div className="mt-1.5">
                      <Select value={dest.credId} onValueChange={(v) => setDest({ ...dest, credId: v })}>
                        <SelectTrigger className="w-full" aria-label="Destination credential"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value={NO_CRED}>Default credential</SelectItem>
                          {objectStoreCreds.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
                        </SelectContent>
                      </Select>
                      <div className="mt-1 text-[10.5px] text-muted-foreground">The object-store credential used to browse and write here. Manage credentials in the Credentials pane.</div>
                      <div className="mt-1 text-[10.5px] text-amber-700 dark:text-amber-300">In an authenticated workspace that started with no object store, external file access is fixed when the Data Playground server starts. Restart the Data Playground server after adding this destination; restarting only the canvas kernel is not enough.</div>
                    </div>
                  )}
                </Section>}

                {canGlobal && active === 'credentials' && <Section id="credentials" title="Credentials">
                  <p className="mb-2 text-[11.5px] leading-relaxed text-muted-foreground">
                    Named credentials a destination or the agent references. Fields store references (`env:VAR` / `file:/path`), never the secret bytes.
                  </p>
                  <div className="mb-3 flex flex-col gap-1">
                    {creds.map((c) => (
                      <div key={c.id} className="flex items-center gap-2 text-xs text-foreground">
                        <span className="flex items-center text-muted-foreground"><Icon name="link" size={12} /></span>
                        <span className="font-semibold">{c.name}</span>
                        <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">{c.kind === 'object_store' ? 'object store' : 'agent'}</Badge>
                        {c.kind === 'object_store' && g.defaultObjectStoreCredId === c.id && <Badge variant="secondary" className="rounded px-1.5 py-0 text-[10px] font-normal">default</Badge>}
                        <span className="flex-1" />
                        {c.kind === 'object_store' && g.defaultObjectStoreCredId !== c.id && (
                          <button onClick={() => setG((prev) => ({ ...prev, defaultObjectStoreCredId: c.id }))}
                            className="text-[10.5px] text-muted-foreground transition-colors hover:text-foreground">Make default</button>
                        )}
                        <button onClick={() => editCred(c)} aria-label={`Edit credential ${c.name}`}
                          className="grid place-items-center text-muted-foreground transition-colors hover:text-foreground"><Icon name="rename" size={12} /></button>
                        <button onClick={() => removeCred(c)} aria-label={`Remove credential ${c.name}`}
                          className="grid place-items-center text-muted-foreground transition-colors hover:text-foreground"><Icon name="close" size={12} /></button>
                      </div>
                    ))}
                    {creds.length === 0 && <div className="text-[11.5px] text-muted-foreground">No credentials yet.</div>}
                  </div>

                  <div className="rounded-md border border-border p-3">
                    <div className="mb-2 text-[12px] font-semibold text-foreground">{credForm.id ? 'Edit credential' : 'New credential'}</div>
                    <div className="mb-2 flex gap-1.5">
                      <Input value={credForm.name} onChange={(e) => setCredForm((p) => ({ ...p, name: e.target.value }))} placeholder="Name" className="min-w-0 flex-1" aria-label="Credential name" />
                      <Select value={credForm.kind} onValueChange={(v) => setCredForm({ id: credForm.id, name: credForm.name, kind: v as CredKind, fields: {} })}>
                        <SelectTrigger className="w-[130px] shrink-0" aria-label="Credential kind"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="object_store">object store</SelectItem>
                          <SelectItem value="agent">agent</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    {credForm.kind === 'object_store' ? (
                      <div className="grid grid-cols-2 gap-1.5">
                        {OBJECT_STORE_FIELDS.map((f) => (
                          <Input key={f.key} value={credForm.fields[f.key] ?? ''} placeholder={f.placeholder} aria-label={f.key}
                            onChange={(e) => setCredField(f.key, e.target.value)} />
                        ))}
                      </div>
                    ) : (
                      <Input value={credForm.fields.apiKey ?? ''} placeholder="env:ANTHROPIC_API_KEY or file:/run/secrets/agent_key" aria-label="apiKey"
                        onChange={(e) => setCredField('apiKey', e.target.value)} />
                    )}
                    <div className="mt-1.5 text-[10.5px] text-muted-foreground">References only (`env:VAR` / `file:/path`). A blank field is left unchanged; leave all blank to use the environment.</div>
                    <div className="mt-2 flex gap-1.5">
                      <Button onClick={saveCred} disabled={!credForm.name.trim()} className="shrink-0">{credForm.id ? 'Save credential' : 'Add credential'}</Button>
                      {credForm.id && <Button variant="outline" onClick={() => setCredForm(emptyCredForm(credForm.kind))} className="shrink-0">Cancel</Button>}
                    </div>
                  </div>
                </Section>}

                {canGlobal && active === 'plugins' && <Section id="plugins" title="Plugins">
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
                        const storedRef = f.secret
                          ? (g[`plugin.${p.name}.${f.key}`] ?? p.config_values?.[f.key])
                          : p.config_values?.[f.key]
                        const ph = f.placeholder ?? (f.secret
                          ? (isSet ? String(storedRef ?? 'env:VAR or file:/path') : 'env:VAR or file:/path')
                          : (f.default != null ? String(f.default) : ''))
                        return (
                          <Field key={f.key} label={f.label}>
                            {f.type === 'select' && f.options ? (
                              <Select value={pval(p.name, f.key, p.config_values?.[f.key])} onValueChange={(v) => setPval(p.name, f.key, v)}>
                                <SelectTrigger aria-label={f.label}><SelectValue placeholder={ph} /></SelectTrigger>
                                <SelectContent>{f.options.map((o) => <SelectItem key={o} value={o}>{o}</SelectItem>)}</SelectContent>
                              </Select>
                            ) : f.type === 'bool' ? (
                              <Select value={pval(p.name, f.key, p.config_values?.[f.key]) || 'false'} onValueChange={(v) => setPval(p.name, f.key, v)}>
                                <SelectTrigger aria-label={f.label}><SelectValue /></SelectTrigger>
                                <SelectContent><SelectItem value="true">true</SelectItem><SelectItem value="false">false</SelectItem></SelectContent>
                              </Select>
                            ) : (
                              <Input
                                type={f.type === 'int' || f.type === 'float' ? 'number' : 'text'}
                                value={f.secret
                                  ? (pcfg[p.name]?.[f.key] ?? (storedRef == null ? '' : String(storedRef)))
                                  : pval(p.name, f.key, p.config_values?.[f.key])}
                                placeholder={ph}
                                aria-label={f.label}
                                onChange={(e) => setPval(p.name, f.key, e.target.value)}
                              />
                            )}
                            {f.secret && <div className="mt-1 text-[10.5px] text-muted-foreground">Secret reference only (`env:VAR` / `file:/path`). Blank on save leaves the stored reference unchanged.</div>}
                            {f.help && <div className="mt-1 text-[10.5px] text-muted-foreground">{f.help}</div>}
                          </Field>
                        )
                      })}
                    </div>
                  ))}
                  {configurable.length === 0 && <div className="text-[11.5px] text-muted-foreground">No plugin declares configurable settings.</div>}
                </Section>}

                {canGlobal && active === 'members' && <Section id="members" title="Members">
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
                </Section>}
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
