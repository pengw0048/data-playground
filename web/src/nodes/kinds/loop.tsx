import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'
import { color } from '../../theme/tokens'

function Loop({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const maxIters = Number(data.config.maxIters ?? 5)
  const budget = Number(data.config.budgetUsd ?? 20)
  return (
    <NodeCard id={id} data={data} metaOverride={`bounded · ≤${maxIters} iters · $${budget} budget`}>
      <div style={{ display: 'flex', gap: 8 }}>
        <Field label="max iters" style={{ flex: 1 }}>
          <MiniInput value={String(maxIters)} onChange={(v) => updateConfig(id, { maxIters: Number(v) || 1 })} />
        </Field>
        <Field label="budget $" style={{ flex: 1 }}>
          <MiniInput value={String(budget)} onChange={(v) => updateConfig(id, { budgetUsd: Number(v) || 0 })} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'loop',
    title: 'loop',
    category: 'control',
    tag: 'loop',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'bounded iterate over an encapsulated subgraph',
    defaultData: () => ({ title: 'loop', status: 'draft', config: { maxIters: 5, budgetUsd: 20 }, meta: 'bounded loop', needsFullPass: true }),
  },
  Loop,
)
