import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field } from '../../ui/controls'
import { ColumnCombo, useInputColumns } from '../fields'

function Dedup({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const on = String(data.config.on ?? '')
  const columns = useInputColumns(id)
  return (
    <NodeCard id={id} data={data} metaOverride={on ? `distinct on ${on}` : 'distinct rows'}>
      <Field label="on columns (blank = all)">
        <ColumnCombo value={on} columns={columns} placeholder="user_id" onChange={(v) => updateConfig(id, { on: v })} />
      </Field>
    </NodeCard>
  )
}

register(
  {
    kind: 'dedup', title: 'dedup', category: 'shape', tag: 'dedup',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }], canBypass: true,
    blurb: 'distinct rows (hash-based, spillable)',
    defaultData: () => ({ title: 'dedup', status: 'draft', config: { on: '' }, meta: 'distinct rows' }),
  },
  Dedup,
)
