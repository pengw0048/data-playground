import { useEffect, useState } from 'react'
import { useStore } from '../store/graph'
import { capabilitiesFor } from '../nodes/registry'
import { Icon } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { ColumnSchema } from '../types/graph'

const PAGE = 50

export function DataPanel({ nodeId }: { nodeId: string }) {
  const preview = useStore((s) => s.previews[nodeId])
  const runPreview = useStore((s) => s.runPreview)
  const requestRun = useStore((s) => s.requestRun)
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === nodeId))
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
  // gate the scalar/chart views on the NODE TYPE, not a column-name heuristic — otherwise any
  // 2-column dataset that happens to have columns named 'metric'+'value' was hijacked (F42).
  const isMetric = node?.type === 'metric'
  const isChart = node?.type === 'chart'
  const special = isMetric || isChart
  const tabs = [{ id: 'rows', label: 'Rows' }, ...caps.map((c) => ({ id: c.id, label: c.label }))]
  // a refresh may drop the capability whose tab was selected — fall back to Rows
  const activeTab = tab === 'rows' || caps.some((c) => c.id === tab) ? tab : 'rows'
  const atEnd = !res.hasMore  // the kernel peeks one extra row, so this is right even at exact multiples

  return (
    <div className="dp-dark text-foreground">
      {/* tab bar + row-count */}
      <div className="flex items-center gap-1.5 border-b border-border px-[11px] py-2">
        {!special && detail == null && tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              'rounded-md px-2.5 py-1 text-[11.5px] font-semibold',
              activeTab === t.id ? 'bg-primary/10 text-primary' : 'text-muted-foreground',
            )}
          >
            {t.label}
          </button>
        ))}
        {detail != null && (
          <button onClick={() => setDetail(null)} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] font-semibold text-primary">
            <Icon name="chevronLeft" size={12} /> Row {offset + detail}
          </button>
        )}
        <span className="flex-1" />
        {!special && detail == null && (
          <>
            <span className="text-[10.5px] text-muted-foreground">
              rows {res.rows.length ? offset : 0}–{offset + res.rows.length}
              {res.truncated && <span className="ml-1.5 rounded bg-muted px-1.5 py-px">sample</span>}
            </span>
            {activeTab === 'rows' && (
              <span className="ml-1 inline-flex gap-0.5">
                <PageBtn dir="prev" disabled={offset === 0} onClick={() => page(Math.max(0, offset - PAGE))} />
                <PageBtn dir="next" disabled={atEnd} onClick={() => page(offset + PAGE)} />
              </span>
            )}
          </>
        )}
      </div>

      {isChart ? (
        <ChartView rows={res.rows} type={String(node?.data.config.chartType ?? 'bar')}
          xLabel={String(node?.data.config.x ?? 'x')}
          yLabel={String(node?.data.config.agg && node?.data.config.agg !== 'none' ? `${node?.data.config.agg}(${node?.data.config.y ?? '*'})` : (node?.data.config.y ?? 'y'))} />
      ) : isMetric ? (
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
      className={cn(
        'grid h-5 w-[22px] place-items-center rounded-[5px]',
        disabled ? 'cursor-default text-muted-foreground/40' : 'cursor-pointer text-muted-foreground',
      )}>
      <Icon name={dir === 'prev' ? 'chevronLeft' : 'chevronRight'} size={13} />
    </button>
  )
}

// Full detail for one row — every column with its full value (untruncated array / url / etc.).
function RowDetail({ columns, row }: { columns: ColumnSchema[]; row: Record<string, unknown> }) {
  return (
    <div className="max-h-[440px] overflow-auto py-1">
      {columns.map((c) => (
        <div key={c.name} className="flex gap-2.5 border-b border-border px-3 py-2">
          <div className="w-[130px] flex-[0_0_130px]">
            <div className="break-words text-[11.5px] font-semibold text-foreground">{c.name}</div>
            <div className="text-[9.5px] text-muted-foreground">{c.type}</div>
          </div>
          <div className="min-w-0 flex-1 text-[11.5px]">
            {c.capabilities.includes('media') && row[c.name] != null && (
              <img src={String(row[c.name])} loading="lazy" className="mb-1.5 block max-h-[140px] max-w-[200px] rounded-md bg-muted" onError={(e) => (e.currentTarget.style.display = 'none')} />
            )}
            <div className="dp-mono whitespace-pre-wrap break-words text-foreground">
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
    <div className="max-h-[440px] overflow-auto">
      <table className="w-full border-collapse text-[11px]">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.name} className="sticky top-0 whitespace-nowrap border-b border-border bg-muted px-2.5 py-[7px] text-left font-semibold text-muted-foreground">
                {c.name}
                {c.capabilities.includes('media') && <span title="media column — thumbnails in the Media tab" className="ml-[5px] cursor-help opacity-60">▦</span>}
                {c.capabilities.includes('vector') && <span title="vector / embedding column" className="ml-[5px] cursor-help opacity-60">⋮⋮</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} onClick={() => onRowClick(i)} title="Click for row detail"
              className="cursor-pointer border-b border-border hover:bg-muted">
              {columns.map((c) => (
                <td key={c.name} className={cn('max-w-[260px] overflow-hidden text-ellipsis whitespace-nowrap px-2.5 py-1.5', c.type.includes('[]') && 'dp-mono')}>
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
  if (value == null) return <span className="text-muted-foreground/60">·</span>
  if (col.capabilities.includes('media')) {
    const url = String(value)
    return (
      <span className="inline-flex items-center gap-1.5">
        <img src={url} loading="lazy" className="h-6 w-[34px] rounded-sm bg-muted object-cover" onError={(e) => (e.currentTarget.style.display = 'none')} />
        <span className="max-w-[150px] overflow-hidden text-ellipsis text-muted-foreground">{url.split('/').slice(-1)[0]}</span>
      </span>
    )
  }
  if (col.capabilities.includes('vector') && Array.isArray(value)) {
    return <span className="rounded bg-primary/10 px-1.5 py-px text-[10px] font-semibold text-primary">[{(value as number[]).length}]</span>
  }
  if (Array.isArray(value)) return <span>[{value.length}]</span>
  if (value === true) return <span className="text-[#2f9e5f]">true</span>
  if (value === false) return <span className="text-destructive">false</span>
  return <span>{String(value)}</span>
}

// A dependency-free SVG chart of the (x, y) series the `chart` node emits — bar / line / area /
// scatter. Colors are theme tokens so it works in dark mode; the axis labels come from the node's
// chosen columns. Kept simple on purpose (the heavy lifting — grouping/aggregation — is server-side).
function ChartView({ rows, type, xLabel, yLabel }: { rows: Record<string, unknown>[]; type: string; xLabel: string; yLabel: string }) {
  const pts = rows.map((r) => ({ x: r.x, y: Number(r.y) })).filter((p) => Number.isFinite(p.y))
  if (!pts.length) return <div className="px-4 py-10 text-center text-[12px] text-muted-foreground">No data to chart — pick X/Y columns.</div>
  const W = 640, H = 320, padL = 48, padR = 16, padT = 16, padB = 44
  const plotW = W - padL - padR, plotH = H - padT - padB
  const ys = pts.map((p) => p.y)
  // bar/area fill to the zero baseline (0 must be in range); line/scatter scale to the DATA range so
  // a far-from-zero or all-negative series isn't squashed into a flat band at one edge.
  const baseline = type === 'bar' || type === 'area'
  const dMax = Math.max(...ys), dMin = Math.min(...ys)
  const yMax = baseline ? Math.max(0, dMax) : dMax, yMin = baseline ? Math.min(0, dMin) : dMin
  const ySpan = (yMax - yMin) || 1
  const yPix = (v: number) => padT + plotH - ((v - yMin) / ySpan) * plotH
  const y0 = yPix(Math.min(Math.max(0, yMin), yMax))  // 0 clamped into the plotted range → the baseline row
  const numX = pts.every((p) => typeof p.x === 'number')
  const xs = pts.map((p) => Number(p.x)), xMin = Math.min(...xs), xMax = Math.max(...xs), xSpan = xMax - xMin || 1
  const xPix = (i: number) => (type === 'scatter' && numX)
    ? padL + ((xs[i] - xMin) / xSpan) * plotW
    : (pts.length === 1 ? padL + plotW / 2 : padL + (i / (pts.length - 1)) * plotW)
  const fmt = (v: number) => (Math.abs(v) >= 1000 || (v !== 0 && Math.abs(v) < 0.01) ? v.toExponential(1) : (Math.round(v * 100) / 100).toString())
  const line = pts.map((p, i) => `${xPix(i)},${yPix(p.y)}`).join(' ')
  const barW = Math.max(2, (plotW / pts.length) * 0.7)
  const tickIdx = Array.from(new Set([0, ...Array.from({ length: Math.min(8, pts.length) }, (_, k) => Math.round(k * (pts.length - 1) / Math.max(1, Math.min(8, pts.length) - 1)))]))

  return (
    <div className="p-3">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 340 }} role="img" aria-label={`${type} chart`}>
        {/* y axis: zero/baseline + min/max labels */}
        <line x1={padL} y1={padT} x2={padL} y2={padT + plotH} stroke="hsl(var(--border))" />
        <line x1={padL} y1={y0} x2={W - padR} y2={y0} stroke="hsl(var(--border))" />
        {[yMax, yMin].map((v, k) => (
          <text key={k} x={padL - 6} y={yPix(v) + 3} textAnchor="end" fontSize="10" fill="hsl(var(--muted-foreground))">{fmt(v)}</text>
        ))}
        {(type === 'bar') && pts.map((p, i) => (
          <rect key={i} x={xPix(i) - barW / 2} y={Math.min(yPix(p.y), y0)} width={barW}
            height={Math.abs(yPix(p.y) - y0)} fill="hsl(var(--primary))" opacity={0.85} />
        ))}
        {(type === 'area') && <polygon points={`${padL},${y0} ${line} ${xPix(pts.length - 1)},${y0}`} fill="hsl(var(--primary))" opacity={0.2} />}
        {(type === 'line' || type === 'area') && <polyline points={line} fill="none" stroke="hsl(var(--primary))" strokeWidth={1.75} />}
        {(type === 'scatter' || type === 'line' || type === 'area') && pts.map((p, i) => (
          <circle key={i} cx={xPix(i)} cy={yPix(p.y)} r={type === 'scatter' ? 3 : 2.2} fill="hsl(var(--primary))" opacity={0.85} />
        ))}
        {/* x tick labels */}
        {tickIdx.map((i) => (
          <text key={i} x={xPix(i)} y={padT + plotH + 16} textAnchor="middle" fontSize="10" fill="hsl(var(--muted-foreground))">
            {String(pts[i]?.x).slice(0, 10)}
          </text>
        ))}
        <text x={padL + plotW / 2} y={H - 4} textAnchor="middle" fontSize="10.5" fill="hsl(var(--muted-foreground))" fontWeight="600">{xLabel}</text>
      </svg>
      <div className="mt-1 text-center text-[10.5px] text-muted-foreground">{yLabel} vs {xLabel} · {pts.length} point{pts.length === 1 ? '' : 's'}</div>
    </div>
  )
}

function MetricValue({ rows }: { rows: Record<string, unknown>[] }) {
  const v = rows[0]?.value
  return (
    <div className="px-4 py-7 text-center">
      <div className="text-[34px] font-bold text-foreground">{typeof v === 'number' ? v.toLocaleString() : String(v)}</div>
      <div className="mt-1.5 text-[11px] text-muted-foreground">{String(rows[0]?.metric ?? 'metric')} · over the full dataset</div>
    </div>
  )
}

function Skeleton() {
  return (
    <div className="dp-dark p-4">
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="my-2.5 h-3 rounded bg-muted" style={{ width: `${90 - i * 8}%`, animation: 'dp-pulse 1.2s infinite' }} />
      ))}
    </div>
  )
}

function ErrorState({ reason, onRetry }: { reason: string; onRetry: () => void }) {
  return (
    <div className="dp-dark px-5 py-6 text-center text-muted-foreground">
      <div className="mb-3 inline-grid h-10 w-10 place-items-center rounded-[10px] bg-destructive/10 text-destructive">
        <Icon name="close" size={18} />
      </div>
      <div className="text-[13px] font-semibold text-destructive">Preview failed</div>
      <div className="dp-mono mx-auto mt-2 max-w-[380px] whitespace-pre-wrap rounded-lg border border-destructive/20 bg-destructive/10 p-2.5 text-left text-[11px] leading-normal text-muted-foreground">{reason}</div>
      <Button variant="outline" size="sm" onClick={onRetry} className="mt-3.5">Retry</Button>
    </div>
  )
}

function NotPreviewable({ reason, onRun }: { reason: string; onRun: () => void }) {
  return (
    <div className="px-5 py-7 text-center text-muted-foreground">
      <div className="mb-3 inline-grid h-10 w-10 place-items-center rounded-[10px] bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-300">
        <Icon name="power" size={18} />
      </div>
      <div className="text-[13px] font-semibold text-foreground">Not sample-previewable</div>
      <div className="mx-auto mt-[5px] max-w-[320px] text-[11.5px] leading-normal">{reason}</div>
      <Button variant="outline" size="sm" onClick={onRun} className="mt-3.5">Run a full pass →</Button>
    </div>
  )
}
