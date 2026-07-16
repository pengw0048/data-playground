import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'

function Sample({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const n = Number(data.config.n ?? 1000)
  const seed = Number(data.config.seed ?? 42)
  return (
    <NodeCard id={id} data={data} metaOverride={`${n.toLocaleString()} rows · seed ${seed}`}>
      <div className="flex gap-2">
        <Field label="n" style={{ flex: 1 }}>
          <MiniInput value={String(n)} onChange={(v) => updateConfig(id, { n: Number(v) || 0 })} />
        </Field>
        <Field label="seed" style={{ flex: 1 }}>
          <MiniInput value={String(seed)} onChange={(v) => updateConfig(id, { seed: Number(v) || 0 })} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'sample',
    title: 'sample',
    category: 'shape',
    tag: 'sample',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset'] }],
    outputs: [{ id: 'out', wire: 'sample' }],
    canBypass: true,
    blurb: 'deterministic seeded reservoir sample',
    defaultData: () => ({ title: 'sample', status: 'draft', config: { n: 1000, seed: 42 }, meta: '1,000 rows · seed 42' }),
  },
  Sample,
)
