import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'

function Sort({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const by = String(data.config.by ?? '')
  return (
    <NodeCard id={id} data={data} metaOverride={by ? `order by ${by}` : 'streaming sort'}>
      <Field label="order by">
        <MiniInput mono value={by} placeholder="score DESC, id" onChange={(v) => updateConfig(id, { by: v })} />
      </Field>
    </NodeCard>
  )
}

register(
  {
    kind: 'sort', title: 'sort', category: 'shape', tag: 'sort',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }], canBypass: true,
    blurb: 'streaming sort (spills)',
    defaultData: () => ({ title: 'sort', status: 'draft', config: { by: '' }, meta: 'streaming sort' }),
  },
  Sort,
)
