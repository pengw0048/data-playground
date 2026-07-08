// Capability viewer tabs (§5.6). A capability adds a viewer tab to any node whose columns qualify; it
// never changes ports. Built-in `media` is registered here; PLUGIN capabilities that declare a
// `viewer` (KernelInfo.capability_views) register the SAME way via syncPluginCapabilities — the plugin
// ships NO frontend code, it just names a generic renderer `kind` (like a NodeSpec names a card).
import type { ComponentType } from 'react'
import { registerCapability } from './registry'
import { Icon } from '../ui/Icon'
import type { ColumnSchema } from '../types/graph'

type TabProps = { columns: ColumnSchema[]; rows: Record<string, unknown>[] }

const colsWith = (cols: ColumnSchema[], capId: string) => cols.filter((c) => c.capabilities.includes(capId))

// generic renderer 'grid' — a media/image grid over the first column tagged with `capId`
function gridTab(capId: string): ComponentType<TabProps> {
  return function GridTab({ columns, rows }: TabProps) {
    const col = colsWith(columns, capId)[0]?.name
    if (!col) return null
    const labelCol = columns.find((c) => !c.capabilities.includes('media') && !c.capabilities.includes('vector'))?.name
    return (
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10, padding: 12 }}>
        {rows.slice(0, 60).map((r, i) => {
          const url = String(r[col] ?? '')
          return (
            <div key={i} style={{ background: 'var(--viewer-2)', borderRadius: 8, overflow: 'hidden', border: '1px solid var(--viewer-line)' }}>
              <div style={{ position: 'relative', aspectRatio: '4/3', background: 'hsl(var(--muted))', display: 'grid', placeItems: 'center' }}>
                <img src={url} loading="lazy" style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                  onError={(e) => { (e.currentTarget.style.display = 'none') }} />
                <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', color: '#c2c6cd', pointerEvents: 'none' }}>
                  <Icon name="play" size={22} />
                </div>
              </div>
              <div style={{ padding: '6px 8px', fontSize: 10.5, color: 'var(--viewer-text-2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {labelCol ? String(r[labelCol]) : url.split('/').slice(-2).join('/')}
              </div>
            </div>
          )
        })}
      </div>
    )
  }
}

// generic renderer 'json' — pretty-print the first column tagged with `capId`, one cell per row
function jsonTab(capId: string): ComponentType<TabProps> {
  return function JsonTab({ columns, rows }: TabProps) {
    const col = colsWith(columns, capId)[0]?.name
    if (!col) return null
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 12 }}>
        {rows.slice(0, 60).map((r, i) => {
          const v = r[col]
          let text: string
          try { text = JSON.stringify(typeof v === 'string' ? JSON.parse(v) : v, null, 2) }
          catch { text = String(v ?? '') }
          return (
            <pre key={i} style={{ margin: 0, padding: '8px 10px', fontSize: 11, lineHeight: 1.5, background: 'var(--viewer-2)',
              border: '1px solid var(--viewer-line)', borderRadius: 6, color: 'var(--viewer-text)', overflowX: 'auto', whiteSpace: 'pre' }}>
              {text}
            </pre>
          )
        })}
      </div>
    )
  }
}

// the generic renderers a plugin's `viewer.kind` can name — no frontend code needed per plugin
const GENERIC_VIEWERS: Record<string, (capId: string) => ComponentType<TabProps>> = {
  grid: gridTab,
  json: jsonTab,
}

// built-in: a media grid over columns tagged `media`
registerCapability({ id: 'media', label: 'Media', predicate: (cols) => colsWith(cols, 'media').length > 0, viewerTab: gridTab('media') })

// register a generic viewer tab for each PLUGIN capability that declares one (from KernelInfo.capability_views).
// An unknown `kind` is skipped (the detector still tags columns; there's just no tab). Called after bootstrap.
export function syncPluginCapabilities(views: { id: string; label: string; viewer: { kind: string } }[]): void {
  for (const v of views) {
    const make = GENERIC_VIEWERS[v.viewer?.kind]
    if (!make) continue
    registerCapability({ id: v.id, label: v.label, predicate: (cols) => colsWith(cols, v.id).length > 0, viewerTab: make(v.id) })
  }
}
