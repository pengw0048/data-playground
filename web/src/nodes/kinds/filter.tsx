import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { Field } from '../../ui/controls'
import { FilterBuilder } from '../fields'

function Filter({ id, data }: NodeComponentProps) {
  const pred = String(data.config.predicate ?? '')
  return (
    <NodeCard id={id} data={data} metaOverride={pred ? `where ${pred}` : 'row predicate'}>
      <Field label="predicate">
        <FilterBuilder nodeId={id} />
      </Field>
    </NodeCard>
  )
}

register(
  {
    kind: 'filter',
    title: 'filter',
    category: 'shape',
    tag: 'filter',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: true,
    blurb: 'row predicate',
    defaultData: () => ({ title: 'filter', status: 'draft', config: { predicate: '' }, meta: 'row predicate' }),
  },
  Filter,
)
