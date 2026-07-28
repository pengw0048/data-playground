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
  const setJobsQuery = useStore((s) => s.setJobsQuery)
  const canvasId = useStore((s) => s.doc.id)
  const history = node?.data.history ?? EMPTY
  const items = [...history].reverse()
  const currentIndex = node?.data.status === 'latest'
    ? items.findIndex((version) => (
      JSON.stringify(version.config) === JSON.stringify(node.data.config)
    ))
    : -1

  if (items.length === 0) {
    return <div style={{ padding: 16, fontSize: 12, color: color.text3 }}>
      <div>No successful output yet.</div>
      {node?.data.status === 'failed' && (
        <button
          onClick={() => setJobsQuery(new URLSearchParams({
            canvas: canvasId, node: nodeId, status: 'failed',
          }).toString())}
          style={{
            marginTop: 10, padding: '5px 10px', border: `1px solid ${color.border}`,
            borderRadius: 7, background: 'hsl(var(--card))', color: color.focus,
            fontSize: 11, fontWeight: 600, cursor: 'pointer',
          }}
        >
          View in Jobs
        </button>
      )}
    </div>
  }

  return (
    <div style={{ padding: 8 }}>
      {items.map((v, i) => {
        const current = i === currentIndex
        return (
          <div
            key={v.id}
            aria-label={current ? 'Current output version' : 'Output version'}
            style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 8px', borderRadius: 8, borderBottom: i < items.length - 1 ? `1px solid ${color.hairline}` : undefined }}
          >
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: current ? color.running : color.draft }} />
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: color.ink }}>
                {current ? 'Current output' : 'Previous output'}
              </div>
              <div style={{ fontSize: 10.5, color: color.text3 }}>
                {timeAgo(v.ts)}
                {v.rows != null
                  ? ` · ${v.rows.toLocaleString()} ${v.rows === 1 ? 'row' : 'rows'}`
                  : v.outputCount != null
                    ? ` · ${v.outputCount.toLocaleString()} ${v.outputCount === 1 ? 'output' : 'outputs'}`
                    : ''}
              </div>
            </div>
            {!current && (
              <button
                disabled={!canEdit}
                title={canEdit ? 'Restore this output version' : 'View-only canvas'}
                onClick={() => restore(nodeId, v.id)}
                style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '5px 10px', border: `1px solid ${color.border}`, borderRadius: 7, background: 'hsl(var(--card))', color: color.focus, fontSize: 11, fontWeight: 600, opacity: canEdit ? 1 : 0.55, cursor: canEdit ? 'pointer' : 'not-allowed' }}
              >
                <Icon name="refresh" size={12} /> Restore
              </button>
            )}
          </div>
        )
      })}
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
