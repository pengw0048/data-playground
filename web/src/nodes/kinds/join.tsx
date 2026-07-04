import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniSelect } from '../../ui/controls'
import { ColumnCombo, useInputColumns } from '../fields'

function Join({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const on = String(data.config.on ?? '')
  const how = (data.config.how as 'inner' | 'left') ?? 'inner'
  const columns = useInputColumns(id)  // union of left + right port columns
  return (
    <NodeCard id={id} data={data} metaOverride={`${how}${on ? ` · on ${on}` : ''}`}>
      <div style={{ display: 'flex', gap: 8 }}>
        <Field label="on" style={{ flex: 1.4 }}>
          <ColumnCombo value={on} columns={columns} placeholder="key" onChange={(v) => updateConfig(id, { on: v })} />
        </Field>
        <Field label="how" style={{ flex: 1 }}>
          <MiniSelect value={how} onChange={(v) => updateConfig(id, { how: v })} options={[{ value: 'inner', label: 'inner' }, { value: 'left', label: 'left' }]} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'join',
    title: 'join',
    category: 'compute',
    tag: 'join',
    inputs: [
      { id: 'a', label: 'left', wire: 'dataset', accepts: ['dataset', 'sample'] },
      { id: 'b', label: 'right', wire: 'dataset', accepts: ['dataset', 'sample'] },
    ],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'combine two datasets on a key',
    defaultData: () => ({ title: 'join', status: 'draft', config: { how: 'inner', on: '' }, meta: 'inner' }),
  },
  Join,
)
