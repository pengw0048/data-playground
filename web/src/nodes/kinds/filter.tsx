import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'

function Filter({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const pred = String(data.config.predicate ?? '')
  return (
    <NodeCard id={id} data={data} metaOverride={pred ? `where ${pred}` : 'row predicate'}>
      <Field label="predicate">
        <MiniInput mono value={pred} placeholder="is_valid == True" onChange={(v) => updateConfig(id, { predicate: v })} />
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
