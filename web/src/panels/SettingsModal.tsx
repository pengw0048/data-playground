import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { useStore } from '../store/graph'
import { color, radius, shadow } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'

// App / workspace settings — a full-screen page with a left category nav (like Figma / most apps),
// not a cramped modal. These are GLOBAL: the LLM agent (provider-agnostic; the key lives in the
// kernel), the execution backend, datasets, connected repos. Canvas-scoped settings live in the
// separate CanvasSettingsModal (opened from the file menu).
const CATS: { id: string; label: string; icon: IconName }[] = [
  { id: 'agent', label: 'Agent', icon: 'sparkle' },
  { id: 'execution', label: 'Execution', icon: 'play' },
  { id: 'datasets', label: 'Datasets', icon: 'db' },
  { id: 'destinations', label: 'Destinations', icon: 'export' },
  { id: 'repos', label: 'Repositories', icon: 'link' },
]

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const kernelInfo = useStore((s) => s.kernelInfo)
  const catalog = useStore((s) => s.catalog)
  const refreshCatalog = useStore((s) => s.refreshCatalog)
  const [g, setG] = useState<Record<string, unknown>>({})
  const [loading, setLoading] = useState(true)
  const [savedMsg, setSavedMsg] = useState('')
  const [dsUri, setDsUri] = useState('')
  const [dsErr, setDsErr] = useState('')
  const [repo, setRepo] = useState({ name: '', url: '' })
  const [dest, setDest] = useState({ name: '', backend: 'local', root: '' })
  const [active, setActive] = useState('agent')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api.getSettings().then((s) => setG(s.global)).catch(() => {}).finally(() => setLoading(false))
  }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const val = (k: string) => (g[k] == null ? '' : String(g[k]))
  const set = (k: string, v: string) => setG((prev) => ({ ...prev, [k]: v }))
  const repos = (Array.isArray(g.connectedRepos) ? g.connectedRepos : []) as { name: string; url: string }[]
  const dests = (Array.isArray(g.destinations) ? g.destinations : []) as { id: string; name: string; backend: string; root: string }[]
  const save = async () => {
    for (const k of ['agentModel', 'agentApiKey', 'agentBaseUrl', 'backend']) {
      await api.putSetting('global', k, g[k] ?? '').catch(() => {})
    }
    await api.putSetting('global', 'connectedRepos', repos).catch(() => {})
    await api.putSetting('global', 'destinations', dests).catch(() => {})
    setSavedMsg('Saved'); setTimeout(() => setSavedMsg(''), 1400)
  }
  const addDest = () => {
    const name = dest.name.trim(), root = dest.root.trim()
    if (!name || !root) return
    const id = `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${Math.abs(Math.floor(Math.random() * 1e6))}`
    setG((prev) => ({ ...prev, destinations: [...dests, { id, name, backend: dest.backend, root }] }))
    setDest({ name: '', backend: 'local', root: '' })
  }
  const addDataset = async () => {
    const uri = dsUri.trim(); if (!uri) return
    setDsErr('')
    try { await api.registerFile(uri); await refreshCatalog(); setDsUri('') }
    catch (e) { setDsErr((e as Error).message) }
  }
  const addRepo = () => {
    const url = repo.url.trim(); if (!url) return
    setG((prev) => ({ ...prev, connectedRepos: [...repos, { name: repo.name.trim() || url, url }] }))
    setRepo({ name: '', url: '' })
  }
  const go = (id: string) => { setActive(id); document.getElementById(`set-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
  const runners = kernelInfo?.runners ?? ['local-out-of-core']

  return createPortal(
    <div className="dp-modal-overlay" onMouseDown={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(20,22,28,.35)', zIndex: 2000, display: 'grid', placeItems: 'center' }}>
      <div onMouseDown={(e) => e.stopPropagation()}
        style={{ width: 'min(940px, 94vw)', height: 'min(680px, 90vh)', background: '#fff', border: `1px solid ${color.border}`, borderRadius: radius.panel, boxShadow: shadow.panel, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 18px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name="settings" size={15} style={{ color: color.text2 }} />
          <span style={{ fontSize: 15, fontWeight: 700 }}>Settings</span>
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 11.5, color: color.latest }}>{savedMsg}</span>
          <button onClick={save} style={{ padding: '7px 16px', border: 'none', borderRadius: 8, background: color.ink, color: '#fff', fontSize: 12.5, fontWeight: 600 }}>Save</button>
          <button aria-label="Close" onClick={onClose} style={{ width: 28, height: 26, border: 'none', background: 'transparent', color: color.text3, display: 'grid', placeItems: 'center' }}><Icon name="close" size={14} /></button>
        </div>

        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          {/* left category nav */}
          <nav style={{ width: 190, flex: '0 0 190px', borderRight: `1px solid ${color.hairline}`, padding: 12, display: 'flex', flexDirection: 'column', gap: 2 }}>
            {CATS.map((c) => (
              <button key={c.id} onClick={() => go(c.id)}
                style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '8px 10px', border: 'none', borderRadius: 8, cursor: 'pointer', fontSize: 12.5, fontWeight: 500, textAlign: 'left',
                  background: active === c.id ? '#e9ecf2' : 'transparent', color: active === c.id ? color.ink : color.text2 }}>
                <Icon name={c.icon} size={14} /> {c.label}
              </button>
            ))}
          </nav>

          {/* content — all sections rendered; the nav scroll-jumps to them */}
          <div ref={scrollRef} style={{ flex: 1, minWidth: 0, overflowY: 'auto', padding: '18px 22px' }}>
            {loading ? <div style={{ fontSize: 12.5, color: color.text3 }}>loading…</div> : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 26 }}>
                <Section id="agent" title="Agent (LLM)">
                  <p style={{ margin: '0 0 10px', fontSize: 11.5, color: color.text3, lineHeight: 1.5 }}>
                    Provider-agnostic. Pick a model and set the matching provider key — the key lives in the kernel,
                    never the browser. Leave the key blank to use an env var instead.
                  </p>
                  <Field label="Model"><Input value={val('agentModel')} placeholder="anthropic/claude-opus-4-8" onChange={(v) => set('agentModel', v)} /></Field>
                  <div style={{ fontSize: 10.5, color: color.text3, margin: '-4px 0 8px' }}>e.g. anthropic/claude-opus-4-8 · anthropic/claude-sonnet-5 · openai/gpt-5 · google/gemini-2.5-pro · ollama/llama3.3</div>
                  <Field label="API key"><Input type="password" value={val('agentApiKey')} placeholder="sk-… (or leave blank for env)" onChange={(v) => set('agentApiKey', v)} /></Field>
                  <Field label="Base URL (local / self-hosted)"><Input value={val('agentBaseUrl')} placeholder="http://localhost:11434 (ollama, optional)" onChange={(v) => set('agentBaseUrl', v)} /></Field>
                </Section>

                <Section id="execution" title="Execution backend">
                  <Field label="Runner">
                    <select value={val('backend') || runners[0]} onChange={(e) => set('backend', e.target.value)}
                      style={{ width: '100%', fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', background: '#fff', outline: 'none' }}>
                      {runners.map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  </Field>
                  <div style={{ fontSize: 10.5, color: color.text3, marginTop: 4 }}>The default local out-of-core engine (DuckDB/Arrow/Polars). Cluster runners install as plugins.</div>
                </Section>

                <Section id="datasets" title="Datasets">
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 8, maxHeight: 200, overflowY: 'auto' }}>
                    {catalog.map((t) => (
                      <div key={t.id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: color.ink }}>
                        <Icon name="db" size={12} style={{ color: color.text3 }} />
                        <span style={{ fontWeight: 600 }}>{t.name}</span>
                        <span style={{ flex: 1, color: color.text3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 11 }}>{t.uri}</span>
                        {t.rowCount != null && <span style={{ fontSize: 10.5, color: color.text3 }}>{t.rowCount} rows</span>}
                      </div>
                    ))}
                    {catalog.length === 0 && <div style={{ fontSize: 11.5, color: color.text3 }}>No datasets registered.</div>}
                  </div>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <input value={dsUri} onChange={(e) => setDsUri(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') addDataset() }}
                      placeholder="path or uri to a Parquet/CSV/JSON/Arrow/Lance dataset"
                      style={{ flex: 1, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }} />
                    <button onClick={addDataset} style={{ border: 'none', borderRadius: 7, background: color.ink, color: '#fff', fontSize: 12, fontWeight: 600, padding: '0 12px' }}>Register</button>
                  </div>
                  {dsErr && <div style={{ fontSize: 10.5, color: color.failed, marginTop: 4 }}>{dsErr}</div>}
                </Section>

                <Section id="destinations" title="Destinations">
                  <p style={{ margin: '0 0 8px', fontSize: 11.5, color: color.text3, lineHeight: 1.5 }}>
                    Named places to save outputs / open files (a local directory, or an object-store prefix).
                    "Workspace outputs" is always available. Object stores (s3://, gs://) browse via a plugin.
                  </p>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 8 }}>
                    {dests.map((d, i) => (
                      <div key={d.id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: color.ink }}>
                        <Icon name="export" size={12} style={{ color: color.text3 }} />
                        <span style={{ fontWeight: 600 }}>{d.name}</span>
                        <span style={{ fontSize: 10, color: color.text3, background: '#f1f2f4', padding: '1px 6px', borderRadius: 4 }}>{d.backend}</span>
                        <span style={{ flex: 1, color: color.text3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 11 }}>{d.root}</span>
                        <button onClick={() => setG((prev) => ({ ...prev, destinations: dests.filter((_, j) => j !== i) }))}
                          style={{ border: 'none', background: 'transparent', color: color.text3, cursor: 'pointer', display: 'grid', placeItems: 'center' }}><Icon name="close" size={12} /></button>
                      </div>
                    ))}
                    {dests.length === 0 && <div style={{ fontSize: 11.5, color: color.text3 }}>Only the default "Workspace outputs".</div>}
                  </div>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <input value={dest.name} onChange={(e) => setDest({ ...dest, name: e.target.value })} placeholder="e.g. S3 exports"
                      style={{ width: 120, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }} />
                    <select value={dest.backend} onChange={(e) => setDest({ ...dest, backend: e.target.value })}
                      style={{ fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 8px', background: '#fff' }}>
                      <option value="local">local</option>
                      <option value="s3">s3</option>
                      <option value="gs">gs</option>
                    </select>
                    <input value={dest.root} onChange={(e) => setDest({ ...dest, root: e.target.value })} onKeyDown={(e) => { if (e.key === 'Enter') addDest() }}
                      placeholder={dest.backend === 'local' ? '/path/to/dir' : `${dest.backend}://bucket/prefix`}
                      style={{ flex: 1, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }} />
                    <button onClick={addDest} style={{ border: 'none', borderRadius: 7, background: color.ink, color: '#fff', fontSize: 12, fontWeight: 600, padding: '0 12px' }}>Add</button>
                  </div>
                </Section>

                <Section id="repos" title="Connected repositories">
                  <p style={{ margin: '0 0 8px', fontSize: 11.5, color: color.text3, lineHeight: 1.5 }}>
                    Code repositories the workspace can pull processors/nodes from (managed here; integration is a plugin).
                  </p>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 8 }}>
                    {repos.map((r, i) => (
                      <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: color.ink }}>
                        <Icon name="link" size={12} style={{ color: color.text3 }} />
                        <span style={{ fontWeight: 600 }}>{r.name}</span>
                        <span style={{ flex: 1, color: color.text3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 11 }}>{r.url}</span>
                        <button onClick={() => setG((prev) => ({ ...prev, connectedRepos: repos.filter((_, j) => j !== i) }))}
                          style={{ border: 'none', background: 'transparent', color: color.text3, cursor: 'pointer', display: 'grid', placeItems: 'center' }}><Icon name="close" size={12} /></button>
                      </div>
                    ))}
                    {repos.length === 0 && <div style={{ fontSize: 11.5, color: color.text3 }}>None connected.</div>}
                  </div>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <input value={repo.name} onChange={(e) => setRepo({ ...repo, name: e.target.value })} placeholder="name"
                      style={{ width: 110, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }} />
                    <input value={repo.url} onChange={(e) => setRepo({ ...repo, url: e.target.value })} onKeyDown={(e) => { if (e.key === 'Enter') addRepo() }} placeholder="https://github.com/org/repo"
                      style={{ flex: 1, fontSize: 12, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }} />
                    <button onClick={addRepo} style={{ border: 'none', borderRadius: 7, background: color.ink, color: '#fff', fontSize: 12, fontWeight: 600, padding: '0 12px' }}>Add</button>
                  </div>
                </Section>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <div id={`set-${id}`} style={{ scrollMarginTop: 8 }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: color.ink, marginBottom: 12 }}>{title}</div>
      {children}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'block', marginBottom: 10 }}>
      <div style={{ fontSize: 11.5, color: color.text2, marginBottom: 4 }}>{label}</div>
      {children}
    </label>
  )
}

function Input({ value, onChange, placeholder, type }: { value: string; onChange: (v: string) => void; placeholder?: string; type?: string }) {
  return (
    <input type={type} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)}
      style={{ width: '100%', fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }} />
  )
}
