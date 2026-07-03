import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'

function Select({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const expr = String(data.config.select ?? '')
  return (
    <NodeCard id={id} data={data} metaOverride={expr ? 'project / derive' : 'all columns'}>
      <Field label="columns / expressions">
        <MiniInput mono value={expr} placeholder="id, lower(name) AS name, a*b AS area" onChange={(v) => updateConfig(id, { select: v })} />
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
