import { useRef, useState } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'
import { Icon } from '../../ui/Icon'
import { Popover } from '../../ui/Popover'

function Source({ id, data }: NodeComponentProps) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const catalog = useStore((s) => s.catalog)
  const updateConfig = useStore((s) => s.updateConfig)
  const rename = useStore((s) => s.rename)
  const table = catalog.find((t) => t.uri === data.config.uri)

  const meta = table
    ? `${(table.rowCount ?? 0).toLocaleString()} rows · ${table.columns.length} cols · ${table.version ?? 'v1'}`
    : 'pick a table'

  return (
    <NodeCard id={id} data={data} metaOverride={meta}>
      {table ? (
        // a dataset is chosen (its name is the node title) — a quiet "change" affordance, not a
        // form dropdown that looks half-filled-in
        <button
          ref={btnRef}
          onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
          style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '3px 4px', border: 'none', background: 'transparent', color: color.text3, fontSize: 11, cursor: 'pointer' }}
        >
          <Icon name="db" size={12} /> Change dataset
        </button>
      ) : (
        <button
          ref={btnRef}
          onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
          style={{
            display: 'flex', alignItems: 'center', gap: 6, width: '100%', padding: '6px 8px',
            border: `1px solid ${color.border}`, borderRadius: 7, background: '#fff', color: color.text3, fontSize: 11.5,
          }}
        >
          <Icon name="db" size={13} />
          <span style={{ flex: 1, textAlign: 'left' }}>Select dataset</span>
          <Icon name="chevronDown" size={12} />
        </button>
      )}

      <Popover anchorRef={btnRef} open={open} onClose={() => setOpen(false)} width={230}>
        {catalog.length === 0 && (
          <div style={{ padding: 8, fontSize: 11.5, color: color.text3 }}>kernel offline — no catalog</div>
        )}
        {catalog.map((t) => (
          <button
            key={t.id}
            onClick={(e) => {
              e.stopPropagation()
              updateConfig(id, { uri: t.uri, tableId: t.id })
              rename(id, t.name)
              setOpen(false)
            }}
            style={{
              display: 'flex', flexDirection: 'column', gap: 1, width: '100%', textAlign: 'left',
              padding: '7px 9px', border: 'none', background: 'transparent', borderRadius: 7,
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = '#f2f3f5')}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
          >
            <span style={{ fontSize: 12, fontWeight: 600, color: color.ink }}>{t.name}</span>
            <span style={{ fontSize: 10, color: color.text3 }}>
              {(t.rowCount ?? 0).toLocaleString()} rows · {t.columns.length} cols
            </span>
          </button>
        ))}
      </Popover>
    </NodeCard>
  )
}

register(
  {
    kind: 'source',
    title: 'source',
    category: 'io',
    tag: 'dataset',
    inputs: [],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'read a registered dataset',
    defaultData: () => ({ title: 'source', status: 'draft', config: {}, meta: 'pick a table' }),
  },
  Source,
)
