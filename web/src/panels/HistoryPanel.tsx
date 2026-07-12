import { roleCanEdit, useStore } from '../store/graph'
import { color, radius } from '../theme/tokens'
import { Icon } from '../ui/Icon'

const EMPTY: never[] = []

// Per-node history — the params actually used + the data version. Restore = canvas time-travel
// (FR-C5). Restoring re-pins config to a past version and marks it latest.
export function HistoryPanel({ nodeId }: { nodeId: string }) {
  // Select the node (stable ref); deriving `history` here (not in the selector) avoids
  // returning a fresh array from the selector, which loops useSyncExternalStore (React #185).
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
  const restore = useStore((s) => s.restoreVersion)
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const history = node?.data.history ?? EMPTY
  const items = [...history].reverse()

  if (items.length === 0) {
    return <div style={{ padding: 16, fontSize: 12, color: color.text3 }}>No versions yet — run this node to snapshot its output.</div>
  }

  return (
    <div style={{ padding: 8 }}>
      {items.map((v, i) => (
        <div
          key={v.id}
          style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 8px', borderRadius: 8, borderBottom: i < items.length - 1 ? `1px solid ${color.hairline}` : undefined }}
        >
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: i === 0 ? color.running : color.draft }} />
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: color.ink }}>
              {v.label}{i === 0 && <span style={{ marginLeft: 6, fontSize: 10, color: color.running }}>· latest</span>}
            </div>
            <div style={{ fontSize: 10.5, color: color.text3 }}>
              {timeAgo(v.ts)}{v.rows != null ? ` · ${v.rows.toLocaleString()} rows` : ''}
            </div>
          </div>
          <button
            disabled={!canEdit}
            title={canEdit ? 'Restore this version' : 'View-only canvas'}
            onClick={() => restore(nodeId, v.id)}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '5px 10px', border: `1px solid ${color.border}`, borderRadius: 7, background: 'hsl(var(--card))', color: color.focus, fontSize: 11, fontWeight: 600, opacity: canEdit ? 1 : 0.55, cursor: canEdit ? 'pointer' : 'not-allowed' }}
          >
            <Icon name="refresh" size={12} /> Restore
          </button>
        </div>
      ))}
    </div>
  )
}

function timeAgo(ts: number): string {
  const s = Math.max(1, Math.round((Date.now() - ts) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}
