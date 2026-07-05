import { useRef, useState } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'
import { Icon } from '../../ui/Icon'
import { Popover } from '../../ui/Popover'
import { FileDialog } from '../../ui/FileDialog'
import { api } from '../../api/client'

function Source({ id, data }: NodeComponentProps) {
  const [open, setOpen] = useState(false)
  const [dialog, setDialog] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const catalog = useStore((s) => s.catalog)
  const refreshCatalog = useStore((s) => s.refreshCatalog)
  const updateConfig = useStore((s) => s.updateConfig)
  const rename = useStore((s) => s.rename)
  const table = catalog.find((t) => t.uri === data.config.uri)

  // pick a file from a destination (local dir / object store) → register it + use it as this source
  const pickFile = async (uri: string, fname: string) => {
    setDialog(false); setOpen(false)
    try {
      const t = await api.registerFile(uri)
      updateConfig(id, { uri: t.uri, tableId: t.id }); rename(id, t.name); void refreshCatalog()
    } catch {
      updateConfig(id, { uri }); rename(id, fname.replace(/\.[^.]+$/, ''))  // offline / unreadable: still wire the uri
    }
  }

  const meta = table
    ? `${(table.rowCount ?? 0).toLocaleString()} rows · ${table.columns.length} cols · ${table.version ?? 'v1'}`
    : 'pick a table'

  return (
    <NodeCard id={id} data={data} metaOverride={meta}>
      {table ? (
        // show the BOUND dataset name (the node title is separately editable, so it can't be relied on
        // to say what's bound); the row itself is the "change" affordance, uri in the tooltip
        <button
          ref={btnRef}
          title={`${table.name} · ${String(data.config.uri ?? '')}\nClick to change dataset`}
          onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
          style={{ display: 'flex', alignItems: 'center', gap: 6, width: '100%', padding: '6px 8px', border: `1px solid ${color.border}`, borderRadius: 7, background: '#fff', color: color.text2, fontSize: 11.5, cursor: 'pointer' }}
        >
          <Icon name="db" size={13} style={{ color: color.text3 }} />
          <span style={{ flex: 1, textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: color.ink, fontWeight: 500 }}>{table.name}</span>
          <Icon name="chevronDown" size={12} style={{ color: color.text3 }} />
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
        <div style={{ height: 1, background: color.hairline, margin: '4px 0' }} />
        <button onClick={(e) => { e.stopPropagation(); setOpen(false); setDialog(true) }}
          style={{ display: 'flex', alignItems: 'center', gap: 7, width: '100%', textAlign: 'left', padding: '7px 9px', border: 'none', background: 'transparent', borderRadius: 7, fontSize: 12, color: color.focus, cursor: 'pointer' }}
          onMouseEnter={(e) => (e.currentTarget.style.background = '#f2f3f5')} onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}>
          <Icon name="search" size={12} /> Browse files…
        </button>
      </Popover>
      {dialog && <FileDialog mode="open" title="Open a dataset" onClose={() => setDialog(false)} onPick={(r) => pickFile(r.uri, r.name)} />}
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
