import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field } from '../../ui/controls'
import { ColumnCombo, useInputColumns } from '../fields'

function Select({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const expr = String(data.config.select ?? '')
  const columns = useInputColumns(id)
  return (
    <NodeCard id={id} data={data} metaOverride={expr ? 'project / derive' : 'all columns'}>
      <Field label="columns / expressions">
        <ColumnCombo value={expr} columns={columns} placeholder="id, lower(name) AS name, a*b AS area" onChange={(v) => updateConfig(id, { select: v })} />
      </Field>
    </NodeCard>
  )
}

register(
  {
    kind: 'select', title: 'select', category: 'shape', tag: 'select',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }], canBypass: true,
    blurb: 'project / rename / derive columns',
    defaultData: () => ({ title: 'select', status: 'draft', config: { select: '' }, meta: 'all columns' }),
  },
  Select,
)
