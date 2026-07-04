import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { useStore } from '../store/graph'
import { color, radius, shadow } from '../theme/tokens'
import { Icon } from '../ui/Icon'

// System settings (global): the LLM agent's model/key/endpoint (provider-agnostic via LiteLLM) and
// the execution backend. Stored in the metadata DB — no config-file editing. Datasets & connected
// repos will get their own sections next.
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

  useEffect(() => {
    api.getSettings().then((s) => setG(s.global)).catch(() => {}).finally(() => setLoading(false))
  }, [])

  const val = (k: string) => (g[k] == null ? '' : String(g[k]))
  const set = (k: string, v: string) => setG((prev) => ({ ...prev, [k]: v }))
  const repos = (Array.isArray(g.connectedRepos) ? g.connectedRepos : []) as { name: string; url: string }[]
  const save = async () => {
    for (const k of ['agentModel', 'agentApiKey', 'agentBaseUrl', 'backend']) {
      await api.putSetting('global', k, g[k] ?? '').catch(() => {})
    }
    await api.putSetting('global', 'connectedRepos', repos).catch(() => {})
    setSavedMsg('Saved'); setTimeout(() => setSavedMsg(''), 1400)
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

  const runners = kernelInfo?.runners ?? ['local-out-of-core']

  return createPortal(
    <div
      onMouseDown={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(20,22,28,.28)', zIndex: 2000, display: 'grid', placeItems: 'center' }}
    >
      <div
        onMouseDown={(e) => e.stopPropagation()}
        style={{ width: 460, maxWidth: '92vw', maxHeight: '88vh', overflowY: 'auto', background: '#fff', border: `1px solid ${color.border}`, borderRadius: radius.panel, boxShadow: shadow.panel }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 16px', borderBottom: `1px solid ${color.hairline}` }}>
          <Icon name="settings" size={15} style={{ color: color.text2 }} />
          <span style={{ fontSize: 14, fontWeight: 600 }}>Settings</span>
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 11.5, color: color.latest }}>{savedMsg}</span>
          <button onClick={onClose} style={{ width: 26, height: 24, border: 'none', background: 'transparent', color: color.text3, display: 'grid', placeItems: 'center' }}><Icon name="close" size={13} /></button>
        </div>

        {loading ? (
          <div style={{ padding: 24, fontSize: 12.5, color: color.text3 }}>loading…</div>
        ) : (
          <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
            <Section title="Agent (LLM)">
              <p style={{ margin: '0 0 10px', fontSize: 11.5, color: color.text3, lineHeight: 1.5 }}>
                Provider-agnostic via LiteLLM. Pick a model and set the matching provider key — the key
                lives in the kernel, never the browser. Leave the key blank to use an env var instead.
              </p>
              <Field label="Model">
                <Input value={val('agentModel')} placeholder="anthropic/claude-opus-4-8" onChange={(v) => set('agentModel', v)} />
              </Field>
              <div style={{ fontSize: 10.5, color: color.text3, margin: '-4px 0 8px' }}>
                e.g. anthropic/claude-opus-4-8 · openai/gpt-4o · gemini/gemini-1.5-pro · ollama/llama3
              </div>
              <Field label="API key">
                <Input type="password" value={val('agentApiKey')} placeholder="sk-… (or leave blank for env)" onChange={(v) => set('agentApiKey', v)} />
              </Field>
              <Field label="Base URL (local / self-hosted)">
                <Input value={val('agentBaseUrl')} placeholder="http://localhost:11434 (ollama, optional)" onChange={(v) => set('agentBaseUrl', v)} />
              </Field>
            </Section>

            <Section title="Execution backend">
              <Field label="Runner">
                <select
                  value={val('backend') || runners[0]}
                  onChange={(e) => set('backend', e.target.value)}
                  style={{ width: '100%', fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', background: '#fff', outline: 'none' }}
                >
                  {runners.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </Field>
              <div style={{ fontSize: 10.5, color: color.text3, marginTop: 4 }}>
                The default local out-of-core engine (DuckDB/Arrow/Polars). Cluster runners install as plugins.
              </div>
            </Section>

            <Section title="Datasets">
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 8, maxHeight: 140, overflowY: 'auto' }}>
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

            <Section title="Connected repositories">
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

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '12px 16px', borderTop: `1px solid ${color.hairline}` }}>
          <button onClick={onClose} style={{ padding: '8px 14px', border: `1px solid ${color.border}`, borderRadius: 8, background: '#fff', fontSize: 12.5, color: color.text2 }}>Close</button>
          <button onClick={save} style={{ padding: '8px 16px', border: 'none', borderRadius: 8, background: color.ink, color: '#fff', fontSize: 12.5, fontWeight: 600 }}>Save</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3, marginBottom: 8 }}>{title}</div>
      {children}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'block', marginBottom: 8 }}>
      <div style={{ fontSize: 11.5, color: color.text2, marginBottom: 4 }}>{label}</div>
      {children}
    </label>
  )
}

function Input({ value, onChange, placeholder, type }: { value: string; onChange: (v: string) => void; placeholder?: string; type?: string }) {
  return (
    <input
      type={type}
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      style={{ width: '100%', fontSize: 12.5, border: `1px solid ${color.border}`, borderRadius: 7, padding: '7px 9px', outline: 'none' }}
    />
  )
}
