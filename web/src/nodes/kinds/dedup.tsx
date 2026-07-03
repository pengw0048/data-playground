import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'

function Dedup({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const on = String(data.config.on ?? '')
  return (
    <NodeCard id={id} data={data} metaOverride={on ? `distinct on ${on}` : 'distinct rows'}>
      <Field label="on columns (blank = all)">
        <MiniInput mono value={on} placeholder="user_id" onChange={(v) => updateConfig(id, { on: v })} />
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
