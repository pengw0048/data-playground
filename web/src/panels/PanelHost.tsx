import { useEffect, useState, type ReactNode } from 'react'
import { useViewport } from '@xyflow/react'
import { useStore, type PanelKind } from '../store/graph'
import { color, radius, shadow } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { DataPanel } from './DataPanel'
import { RunPanel } from './RunPanel'
import { HistoryPanel } from './HistoryPanel'
import { CodePanel } from './CodePanel'
import { LineagePanel } from './LineagePanel'
import { ErrorBoundary } from '../ui/ErrorBoundary'

// Panels anchor 12px below the node's action row, one open per node (§5.2, actions page).
export function PanelHost() {
  const openPanels = useStore((s) => s.openPanels)
  const viewport = useViewport() // re-render on pan/zoom so anchors track
  const [, force] = useState(0)

  // also re-measure a beat after open (DOM settles)
  useEffect(() => {
    const t = setTimeout(() => force((n) => n + 1), 30)
    return () => clearTimeout(t)
  }, [openPanels, viewport.x, viewport.y, viewport.zoom])

  return (
    <>
      {Object.entries(openPanels).map(([nodeId, kind]) => (
        <AnchoredPanel key={nodeId} nodeId={nodeId} kind={kind} />
      ))}
    </>
  )
}

function AnchoredPanel({ nodeId, kind }: { nodeId: string; kind: PanelKind }) {
  const el = document.querySelector<HTMLElement>(`.react-flow__node[data-id="${nodeId}"]`)
  const rect = el?.getBoundingClientRect()
  const title = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId)?.data.title ?? '')
  const close = useStore((s) => s.closePanel)

  if (!rect) return null
  const width = kind === 'data' ? 460 : kind === 'run' ? 340 : kind === 'code' ? 420 : 300
  // Prefer to the RIGHT of the node so the panel never covers it; fall back to below-left.
  const gap = 12
  let left: number
  let top: number
  if (rect.right + gap + width <= window.innerWidth - 12) {
    left = rect.right + gap
    top = rect.top
  } else {
    left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12))
    top = rect.bottom + gap
  }
  // keep the WHOLE panel on-screen: clamp top, then cap the panel's height to the space below it
  top = Math.max(12, top)
  const maxPanelH = Math.min(620, window.innerHeight - top - 16)
  if (maxPanelH < 240) { top = Math.max(12, window.innerHeight - 16 - 240) }
  const panelMaxHeight = Math.min(620, window.innerHeight - top - 16)

  return (
    <div className="dp-float dp-panel" style={{ position: 'fixed', left, top, width, zIndex: 25 }}>
      <div
        style={{
          background: '#fff',
          border: `1px solid ${color.border}`,
          borderRadius: radius.panel, boxShadow: shadow.panel, overflow: 'hidden',
          maxHeight: panelMaxHeight, display: 'flex', flexDirection: 'column',
        }}
      >
        <PanelTitle nodeId={nodeId} title={title} kind={kind} onClose={() => close(nodeId)} />
        <div style={{ overflow: 'auto', flex: 1 }}>
          <ErrorBoundary compact>
            {kind === 'data' && <DataPanel nodeId={nodeId} />}
            {kind === 'run' && <RunPanel nodeId={nodeId} />}
            {kind === 'history' && <HistoryPanel nodeId={nodeId} />}
            {kind === 'code' && <CodePanel nodeId={nodeId} />}
            {kind === 'lineage' && <LineagePanel nodeId={nodeId} />}
          </ErrorBoundary>
        </div>
      </div>
    </div>
  )
}

function PanelTitle({ nodeId, title, kind, dark, onClose }: {
  nodeId: string; title: string; kind: PanelKind; dark?: boolean; onClose: () => void
}) {
  const label = { data: 'data', run: 'run', history: 'history', code: 'code', lineage: 'lineage' }[kind]
  const runPreview = useStore((s) => s.runPreview)
  return (
    <div
      style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '9px 11px',
        borderBottom: `1px solid ${dark ? '#23262d' : color.hairline}`,
        color: dark ? 'var(--viewer-text)' : color.ink,
      }}
    >
      <span style={{ width: 7, height: 7, borderRadius: '50%', background: '#2f9e8f' }} />
      <span style={{ fontSize: 12.5, fontWeight: 600 }}>{title}</span>
      <span style={{ fontSize: 12.5, color: dark ? 'var(--viewer-text-2)' : color.text3 }}>· {label}</span>
      <span style={{ flex: 1 }} />
      {kind === 'data' && (
        <button onClick={() => runPreview(nodeId)} title="Refresh" style={iconBtn(dark)}><Icon name="refresh" size={13} /></button>
      )}
      <button onClick={onClose} title="Close" style={iconBtn(dark)}><Icon name="close" size={13} /></button>
    </div>
  )
}

function iconBtn(dark?: boolean): React.CSSProperties {
  return {
    width: 24, height: 22, display: 'grid', placeItems: 'center', border: 'none',
    borderRadius: 6, background: 'transparent', color: dark ? 'var(--viewer-text-2)' : color.text3,
  }
}
