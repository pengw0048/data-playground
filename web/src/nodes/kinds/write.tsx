import { useState } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput, MiniSelect } from '../../ui/controls'
import { Icon } from '../../ui/Icon'
import { FileDialog } from '../../ui/FileDialog'
import { color } from '../../theme/tokens'

function Write({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const [dialog, setDialog] = useState(false)
  const name = String(data.config.name ?? '')
  const mode = (data.config.writeMode as 'append' | 'overwrite') ?? 'overwrite'
  const destName = data.config.destName as string | undefined
  const destPath = String(data.config.destPath ?? '')
  const where = destName ? `${destName}${destPath ? `/${destPath}` : ''}` : 'Workspace outputs'
  return (
    <NodeCard id={id} data={data} metaOverride={name ? `→ ${name} · ${mode}` : 'name a durable dataset →'}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          <Field label="name" style={{ flex: 1.6 }}>
            <MiniInput value={name} placeholder="output_table" onChange={(v) => updateConfig(id, { name: v })} />
          </Field>
          <Field label="mode" style={{ flex: 1 }}>
            <MiniSelect value={mode} onChange={(v) => updateConfig(id, { writeMode: v })} options={[{ value: 'overwrite', label: 'overwrite' }, { value: 'append', label: 'append' }]} />
          </Field>
        </div>
        {/* where the output goes — a chosen destination place, or the default workspace outputs */}
        <button className="nodrag" onClick={(e) => { e.stopPropagation(); setDialog(true) }}
          style={{ display: 'flex', alignItems: 'center', gap: 6, width: '100%', padding: '6px 8px', border: `1px solid ${color.border}`, borderRadius: 7, background: '#fff', color: color.text2, fontSize: 11, cursor: 'pointer' }}>
          <Icon name="export" size={12} style={{ color: color.text3 }} />
          <span style={{ flex: 1, textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{where}</span>
          <span style={{ color: color.focus, fontWeight: 600 }}>Change</span>
        </button>
      </div>
      {dialog && (
        <FileDialog mode="save" defaultName={name || 'output'}
          onClose={() => setDialog(false)}
          onPick={(r) => { updateConfig(id, { destId: r.destId, destName: r.destName, destPath: r.path, name: r.filename }); setDialog(false) }} />
      )}
    </NodeCard>
  )
}

register(
  {
    kind: 'write',
    title: 'write',
    category: 'io',
    tag: 'write',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample', 'selection'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'materialize / commit to a registered dataset',
    defaultData: () => ({ title: 'write', status: 'draft', config: { writeMode: 'overwrite' }, meta: 'sink · needs full pass', needsFullPass: true }),
  },
  Write,
)
