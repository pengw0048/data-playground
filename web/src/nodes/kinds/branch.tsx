import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'
import { color } from '../../theme/tokens'

function Branch({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const pred = String(data.config.predicate ?? '')
  return (
    <NodeCard id={id} data={data} metaOverride={pred ? `route by ${pred}` : 'route by predicate / metric'}>
      <Field label="predicate → true | false">
        <MiniInput mono value={pred} placeholder="dup_rate > 0.1" onChange={(v) => updateConfig(id, { predicate: v })} />
      </Field>
      <div style={{ display: 'flex', gap: 12, marginTop: 6, fontSize: 10, color: color.text3 }}>
        <span>▲ true</span><span>▼ false</span>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'branch',
    title: 'branch',
    category: 'control',
    tag: 'branch',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample', 'metric'] }],
    outputs: [
      { id: 'true', label: 'true', wire: 'dataset' },
      { id: 'false', label: 'false', wire: 'dataset' },
    ],
    canBypass: false,
    blurb: 'route flow by predicate or metric — no cycle, just a fork',
    defaultData: () => ({ title: 'branch', status: 'draft', config: { predicate: '' }, meta: 'route by predicate' }),
  },
  Branch,
)
