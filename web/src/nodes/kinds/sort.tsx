import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { Field } from '../../ui/controls'
import { SortBuilder } from '../fields'

function Sort({ id, data }: NodeComponentProps) {
  const by = String(data.config.by ?? '')
  return (
    <NodeCard id={id} data={data} metaOverride={by ? `order by ${by}` : 'streaming sort'}>
      <Field label="order by">
        <SortBuilder nodeId={id} />
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
