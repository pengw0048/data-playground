import { useEffect, useState } from 'react'
import { useStore } from '../store/graph'
import { capabilitiesFor } from '../nodes/registry'
import { radius } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import type { ColumnSchema } from '../types/graph'

const PAGE = 50

export function DataPanel({ nodeId }: { nodeId: string }) {
  const preview = useStore((s) => s.previews[nodeId])
  const runPreview = useStore((s) => s.runPreview)
  const requestRun = useStore((s) => s.requestRun)
  const [tab, setTab] = useState('rows')
  const [detail, setDetail] = useState<number | null>(null)  // index of the row whose detail is open
  const offset = preview?.offset ?? 0  // the page is owned by the store, so an external Refresh can't desync it

  useEffect(() => {
    if (!preview) runPreview(nodeId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId])
  const page = (o: number) => { setDetail(null); runPreview(nodeId, o) }

  if (!preview || preview.loading) return <Skeleton />
  if (preview.error) return <ErrorState reason={preview.error} onRetry={() => runPreview(nodeId, offset)} />
  const res = preview.result!
  if (res.error) return <ErrorState reason={res.reason ?? 'preview failed'} onRetry={() => runPreview(nodeId, offset)} />
  if (res.notPreviewable) return <NotPreviewable reason={res.reason ?? 'needs a full pass'} onRun={() => requestRun(nodeId)} />

  const columns = res.columns
  const caps = capabilitiesFor(columns as ColumnSchema[])
  const isMetric = columns.length === 2 && columns.some((c) => c.name === 'value') && columns.some((c) => c.name === 'metric')
  const tabs = [{ id: 'rows', label: 'Rows' }, ...caps.map((c) => ({ id: c.id, label: c.label }))]
  // a refresh may drop the capability whose tab was selected — fall back to Rows
  const activeTab = tab === 'rows' || caps.some((c) => c.id === tab) ? tab : 'rows'
  const atEnd = !res.hasMore  // the kernel peeks one extra row, so this is right even at exact multiples

  return (
    <div className="dp-dark" style={{ color: 'var(--viewer-text)' }}>
      {/* tab bar + row-count */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 11px', borderBottom: '1px solid var(--viewer-line)' }}>
        {!isMetric && detail == null && tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            style={{
              fontSize: 11.5, fontWeight: 600, padding: '4px 10px', border: 'none', borderRadius: 6,
              background: activeTab === t.id ? '#e7ebf5' : 'transparent',
              color: activeTab === t.id ? '#3355c6' : 'var(--viewer-text-2)',
            }}
          >
            {t.label}
          </button>
        ))}
        {detail != null && (
          <button onClick={() => setDetail(null)} style={{ fontSize: 11.5, fontWeight: 600, padding: '4px 8px', border: 'none', borderRadius: 6, background: 'transparent', color: '#3355c6', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <Icon name="chevronLeft" size={12} /> Row {offset + detail}
          </button>
        )}
        <span style={{ flex: 1 }} />
        {!isMetric && detail == null && (
          <>
            <span style={{ fontSize: 10.5, color: 'var(--viewer-text-2)' }}>
              rows {res.rows.length ? offset : 0}–{offset + res.rows.length}
              {res.truncated && <span style={{ marginLeft: 6, background: '#eef0f3', padding: '1px 6px', borderRadius: 4 }}>sample</span>}
            </span>
            {activeTab === 'rows' && (
              <span style={{ display: 'inline-flex', gap: 2, marginLeft: 4 }}>
                <PageBtn dir="prev" disabled={offset === 0} onClick={() => page(Math.max(0, offset - PAGE))} />
                <PageBtn dir="next" disabled={atEnd} onClick={() => page(offset + PAGE)} />
              </span>
            )}
          </>
        )}
      </div>

      {isMetric ? (
        <MetricValue rows={res.rows} />
      ) : detail != null && res.rows[detail] ? (
        <RowDetail columns={columns as ColumnSchema[]} row={res.rows[detail]} />
      ) : activeTab === 'rows' ? (
        <RowsTable columns={columns as ColumnSchema[]} rows={res.rows} onRowClick={setDetail} />
      ) : (
        (() => {
          const cap = caps.find((c) => c.id === activeTab)
          const Tab = cap?.viewerTab
          return Tab ? <Tab columns={columns as ColumnSchema[]} rows={res.rows} /> : null
        })()
      )}
    </div>
  )
}

function PageBtn({ dir, disabled, onClick }: { dir: 'prev' | 'next'; disabled: boolean; onClick: () => void }) {
  return (
    <button aria-label={dir === 'prev' ? 'Previous page' : 'Next page'} onClick={onClick} disabled={disabled}
      style={{ width: 22, height: 20, display: 'grid', placeItems: 'center', border: 'none', borderRadius: 5, background: 'transparent', color: disabled ? '#c8ccd2' : 'var(--viewer-text-2)', cursor: disabled ? 'default' : 'pointer' }}>
      <Icon name={dir === 'prev' ? 'chevronLeft' : 'chevronRight'} size={13} />
    </button>
  )
}

// Full detail for one row — every column with its full value (untruncated array / url / etc.).
function RowDetail({ columns, row }: { columns: ColumnSchema[]; row: Record<string, unknown> }) {
  return (
    <div style={{ maxHeight: 440, overflow: 'auto', padding: '4px 0' }}>
      {columns.map((c) => (
        <div key={c.name} style={{ display: 'flex', gap: 10, padding: '8px 12px', borderBottom: '1px solid var(--viewer-line)' }}>
          <div style={{ width: 130, flex: '0 0 130px' }}>
            <div style={{ fontSize: 11.5, fontWeight: 600, color: 'var(--viewer-text)', wordBreak: 'break-word' }}>{c.name}</div>
            <div style={{ fontSize: 9.5, color: 'var(--viewer-text-2)' }}>{c.type}</div>
          </div>
          <div style={{ flex: 1, minWidth: 0, fontSize: 11.5 }}>
            {c.capabilities.includes('media') && row[c.name] != null && (
              <img src={String(row[c.name])} loading="lazy" style={{ maxWidth: 200, maxHeight: 140, borderRadius: 6, display: 'block', marginBottom: 6, background: '#eceef1' }} onError={(e) => (e.currentTarget.style.display = 'none')} />
            )}
            <div className="dp-mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--viewer-text)' }}>
              {row[c.name] == null ? '·' : Array.isArray(row[c.name]) ? JSON.stringify(row[c.name]) : String(row[c.name])}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

function RowsTable({ columns, rows, onRowClick }: { columns: ColumnSchema[]; rows: Record<string, unknown>[]; onRowClick: (i: number) => void }) {
  return (
    <div style={{ overflow: 'auto', maxHeight: 440 }}>
      <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: 11 }}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.name} style={{ textAlign: 'left', padding: '7px 10px', position: 'sticky', top: 0, background: 'var(--viewer-2)', color: 'var(--viewer-text-2)', fontWeight: 600, whiteSpace: 'nowrap', borderBottom: '1px solid var(--viewer-line)' }}>
                {c.name}
                {c.capabilities.includes('media') && <span title="media column — thumbnails in the Media tab" style={{ marginLeft: 5, opacity: .6, cursor: 'help' }}>▦</span>}
                {c.capabilities.includes('vector') && <span title="vector / embedding column" style={{ marginLeft: 5, opacity: .6, cursor: 'help' }}>⋮⋮</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} onClick={() => onRowClick(i)} title="Click for row detail"
              style={{ borderBottom: '1px solid var(--viewer-line)', cursor: 'pointer' }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--viewer-2)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}>
              {columns.map((c) => (
                <td key={c.name} style={{ padding: '6px 10px', whiteSpace: 'nowrap', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis' }} className={c.type.includes('[]') ? 'dp-mono' : undefined}>
                  <Cell col={c} value={r[c.name]} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Cell({ col, value }: { col: ColumnSchema; value: unknown }) {
  if (value == null) return <span style={{ color: '#b0b4bc' }}>·</span>
  if (col.capabilities.includes('media')) {
    const url = String(value)
    return (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
        <img src={url} loading="lazy" style={{ width: 34, height: 24, objectFit: 'cover', borderRadius: 3, background: '#eceef1' }} onError={(e) => (e.currentTarget.style.display = 'none')} />
        <span style={{ color: 'var(--viewer-text-2)', maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis' }}>{url.split('/').slice(-1)[0]}</span>
      </span>
    )
  }
  if (col.capabilities.includes('vector') && Array.isArray(value)) {
    return <span style={{ fontSize: 10, fontWeight: 600, color: '#3355c6', background: '#e7ecfb', padding: '1px 6px', borderRadius: 4 }}>[{(value as number[]).length}]</span>
  }
  if (Array.isArray(value)) return <span>[{value.length}]</span>
  if (value === true) return <span style={{ color: '#2f9e5f' }}>true</span>
  if (value === false) return <span style={{ color: '#d64550' }}>false</span>
  return <span>{String(value)}</span>
}

function MetricValue({ rows }: { rows: Record<string, unknown>[] }) {
  const v = rows[0]?.value
  return (
    <div style={{ padding: '28px 16px', textAlign: 'center' }}>
      <div style={{ fontSize: 34, fontWeight: 700, color: 'var(--viewer-text)' }}>{typeof v === 'number' ? v.toLocaleString() : String(v)}</div>
      <div style={{ marginTop: 6, fontSize: 11, color: 'var(--viewer-text-2)' }}>{String(rows[0]?.metric ?? 'metric')} · over the full dataset</div>
    </div>
  )
}

function Skeleton() {
  return (
    <div className="dp-dark" style={{ padding: 16 }}>
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} style={{ height: 12, background: '#eceef1', borderRadius: 4, margin: '10px 0', width: `${90 - i * 8}%`, animation: 'dp-pulse 1.2s infinite' }} />
      ))}
    </div>
  )
}

function ErrorState({ reason, onRetry }: { reason: string; onRetry: () => void }) {
  return (
    <div className="dp-dark" style={{ padding: '24px 20px', textAlign: 'center', color: 'var(--viewer-text-2)' }}>
      <div style={{ display: 'inline-grid', placeItems: 'center', width: 40, height: 40, borderRadius: 10, background: '#fbeef0', color: '#d64550', marginBottom: 12 }}>
        <Icon name="close" size={18} />
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, color: '#d64550' }}>Preview failed</div>
      <div className="dp-mono" style={{ fontSize: 11, marginTop: 8, lineHeight: 1.5, maxWidth: 380, marginInline: 'auto', color: 'var(--viewer-text-2)', whiteSpace: 'pre-wrap', textAlign: 'left', background: '#faf1f2', border: '1px solid #f0dcdf', borderRadius: 8, padding: 10 }}>{reason}</div>
      <button onClick={onRetry} style={{ marginTop: 14, padding: '7px 16px', border: '1px solid var(--viewer-line)', borderRadius: 8, background: '#fff', color: 'var(--viewer-text)', fontSize: 12, fontWeight: 600 }}>Retry</button>
    </div>
  )
}

function NotPreviewable({ reason, onRun }: { reason: string; onRun: () => void }) {
  return (
    <div className="dp-dark" style={{ padding: '28px 20px', textAlign: 'center', color: 'var(--viewer-text-2)' }}>
      <div style={{ display: 'inline-grid', placeItems: 'center', width: 40, height: 40, borderRadius: 10, background: '#fbf1dc', color: '#d99a2b', marginBottom: 12 }}>
        <Icon name="power" size={18} />
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--viewer-text)' }}>Not sample-previewable</div>
      <div style={{ fontSize: 11.5, marginTop: 5, lineHeight: 1.5, maxWidth: 320, marginInline: 'auto' }}>{reason}</div>
      <button
        onClick={onRun}
        style={{ marginTop: 14, padding: '7px 16px', border: '1px solid var(--viewer-line)', borderRadius: 8, background: '#fff', color: 'var(--viewer-text)', fontSize: 12, fontWeight: 600 }}
      >
        Run a full pass →
      </button>
    </div>
  )
}
