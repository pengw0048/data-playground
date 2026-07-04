import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniSelect } from '../../ui/controls'
import { ColumnCombo, useInputColumns } from '../fields'

type Agg = 'count' | 'mean' | 'sum' | 'min' | 'max'

function Metric({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const agg = (data.config.agg as Agg) ?? 'count'
  const column = String(data.config.column ?? '')
  const columns = useInputColumns(id)
  return (
    <NodeCard id={id} data={data} metaOverride={`${agg}${agg !== 'count' && column ? `(${column})` : ''} · value + sparkline`}>
      <div style={{ display: 'flex', gap: 8 }}>
        <Field label="agg" style={{ flex: 1 }}>
          <MiniSelect<Agg> value={agg} onChange={(v) => updateConfig(id, { agg: v })} options={[{ value: 'count', label: 'count' }, { value: 'mean', label: 'mean' }, { value: 'sum', label: 'sum' }, { value: 'min', label: 'min' }, { value: 'max', label: 'max' }]} />
        </Field>
        <Field label="column" style={{ flex: 1.3 }}>
          <ColumnCombo value={column} columns={columns} placeholder="—" onChange={(v) => updateConfig(id, { column: v })} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'metric',
    title: 'metric',
    category: 'inspect',
    tag: 'metric',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'metric', label: 'value' }],
    canBypass: false,
    blurb: 'reduce to a scalar / series',
    defaultData: () => ({ title: 'metric', status: 'draft', config: { agg: 'count' }, meta: 'count · value + sparkline' }),
  },
  Metric,
)
