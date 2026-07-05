// Built-in capability providers (§5.4). Media + vector are generic, schema-driven, and add
// viewer tabs — they never change a node's ports. Reference-bundle capabilities (e.g.
// column-mirror) would register the same way from a plugin.
import { registerCapability } from './registry'
import { Icon } from '../ui/Icon'
import type { ColumnSchema } from '../types/graph'

function mediaCols(cols: ColumnSchema[]) { return cols.filter((c) => c.capabilities.includes('media')) }

function MediaGrid({ columns, rows }: { columns: ColumnSchema[]; rows: Record<string, unknown>[] }) {
  const mcols = mediaCols(columns)
  const col = mcols[0]?.name
  if (!col) return null
  const labelCol = columns.find((c) => !c.capabilities.includes('media') && !c.capabilities.includes('vector'))?.name
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10, padding: 12 }}>
      {rows.slice(0, 60).map((r, i) => {
        const url = String(r[col] ?? '')
        return (
          <div key={i} style={{ background: 'var(--viewer-2)', borderRadius: 8, overflow: 'hidden', border: '1px solid var(--viewer-line)' }}>
            <div style={{ position: 'relative', aspectRatio: '4/3', background: '#eceef1', display: 'grid', placeItems: 'center' }}>
              <img
                src={url}
                loading="lazy"
                style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                onError={(e) => { (e.currentTarget.style.display = 'none') }}
              />
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

registerCapability({
  id: 'media',
  label: 'Media',
  predicate: (cols) => mediaCols(cols).length > 0,
  viewerTab: MediaGrid,
})

// (A dedicated "Vectors" tab was removed — it wasn't useful; the Rows table shows a [dim] chip and
// clicking a row shows the full vector. Vector columns are still detected for that cell rendering.)
