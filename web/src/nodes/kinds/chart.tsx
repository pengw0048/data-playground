import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniSelect } from '../../ui/controls'
import { ColumnCombo, useInputColumns } from '../fields'

type ChartType = 'bar' | 'line' | 'scatter' | 'area'
type Agg = 'none' | 'count' | 'sum' | 'mean' | 'min' | 'max'

// The `chart` node turns a column pair into a visualization (rendered in the data panel). It builds
// an (x, y) series — grouped `agg(y) by x` for bar/line, or raw x,y points for scatter — so it
// runs out-of-core at scale and chains like any dataset.
function Chart({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const chartType = (data.config.chartType as ChartType) ?? 'bar'
  const agg = (data.config.agg as Agg) ?? 'count'
  const x = String(data.config.x ?? '')
  const y = String(data.config.y ?? '')
  const columns = useInputColumns(id)
  return (
    <NodeCard id={id} data={data} metaOverride={`${chartType} · ${x || '—'}${agg !== 'none' ? ` · ${agg}(${y || '·'})` : y ? ` × ${y}` : ''}`}>
      <div className="flex gap-2">
        <Field label="type" style={{ flex: 1 }}>
          <MiniSelect<ChartType> value={chartType} onChange={(v) => updateConfig(id, { chartType: v })}
            options={[{ value: 'bar', label: 'bar' }, { value: 'line', label: 'line' }, { value: 'scatter', label: 'scatter' }, { value: 'area', label: 'area' }]} />
        </Field>
        <Field label="agg" style={{ flex: 1 }}>
          <MiniSelect<Agg> value={agg} onChange={(v) => updateConfig(id, { agg: v })}
            options={[{ value: 'none', label: 'none' }, { value: 'count', label: 'count' }, { value: 'sum', label: 'sum' }, { value: 'mean', label: 'mean' }, { value: 'min', label: 'min' }, { value: 'max', label: 'max' }]} />
        </Field>
      </div>
      <div className="mt-2 flex gap-2">
        <Field label="X" style={{ flex: 1 }}>
          <ColumnCombo value={x} columns={columns} placeholder="—" onChange={(v) => updateConfig(id, { x: v })} />
        </Field>
        <Field label="Y" style={{ flex: 1 }}>
          <ColumnCombo value={y} columns={columns} placeholder={agg === 'count' ? '(count)' : '—'} onChange={(v) => updateConfig(id, { y: v })} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'chart',
    title: 'chart',
    category: 'inspect',
    tag: 'chart',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'visualize a column pair — grouped bar/line, or raw scatter',
    defaultData: () => ({ title: 'chart', status: 'draft', config: { chartType: 'bar', agg: 'count' }, meta: 'bar · —' }),
  },
  Chart,
)
