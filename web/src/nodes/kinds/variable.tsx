import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'
import { color } from '../../theme/tokens'

function Variable({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const target = String(data.config.column ?? '')
  return (
    <NodeCard id={id} data={data} metaOverride={target ? `→ ${target}` : "a node's output → another node's param"}>
      <Field label="drives param">
        <MiniInput mono value={target} placeholder="param name" onChange={(v) => updateConfig(id, { column: v })} />
      </Field>
    </NodeCard>
  )
}

register(
  {
    kind: 'variable',
    title: 'variable',
    category: 'control',
    tag: 'variable',
    inputs: [{ id: 'in', wire: 'metric', accepts: ['metric', 'sample', 'dataset'] }],
    outputs: [{ id: 'out', wire: 'sample' }],
    canBypass: false,
    blurb: 'a value-port: a metric drives another node’s parameter',
    defaultData: () => ({ title: 'variable', status: 'draft', config: {}, meta: 'value-port' }),
  },
  Variable,
)
