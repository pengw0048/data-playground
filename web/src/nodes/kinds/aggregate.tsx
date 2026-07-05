import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'
import { ColumnCombo, useInputColumns } from '../fields'

function Aggregate({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const group = String(data.config.groupBy ?? '')
  const aggs = String(data.config.aggs ?? 'count(*) AS n')
  const columns = useInputColumns(id)
  return (
    <NodeCard id={id} data={data} metaOverride={`group by ${group || '—'} · needs full pass`}>
      <div className="flex flex-col gap-2">
        <Field label="group by">
          <ColumnCombo value={group} columns={columns} placeholder="category" onChange={(v) => updateConfig(id, { groupBy: v })} />
        </Field>
        <Field label="aggregations">
          <MiniInput mono value={aggs} placeholder="count(*) AS n, avg(x) AS avg_x" onChange={(v) => updateConfig(id, { aggs: v })} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'aggregate', title: 'aggregate', category: 'compute', tag: 'aggregate',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset'] }],
    outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false,
    blurb: 'group-by aggregation (out-of-core)',
    defaultData: () => ({ title: 'aggregate', status: 'draft', config: { aggs: 'count(*) AS n' }, meta: 'group-by · needs full pass', needsFullPass: true }),
  },
  Aggregate,
)
