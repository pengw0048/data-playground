import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniSelect } from '../../ui/controls'

function Union({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const mode = (data.config.mode as 'all' | 'distinct') ?? 'all'
  const align = (data.config.align as 'name' | 'position') ?? 'name'
  return (
    <NodeCard id={id} data={data} metaOverride={`${mode === 'all' ? 'union all' : 'distinct'} · by ${align}`}>
      <div className="flex gap-2">
        <Field label="rows" style={{ flex: 1 }}>
          <MiniSelect value={mode} onChange={(v) => updateConfig(id, { mode: v })}
            options={[{ value: 'all', label: 'all' }, { value: 'distinct', label: 'distinct' }]} />
        </Field>
        <Field label="align by" style={{ flex: 1 }}>
          <MiniSelect value={align} onChange={(v) => updateConfig(id, { align: v })}
            options={[{ value: 'name', label: 'name' }, { value: 'position', label: 'position' }]} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'union', title: 'union', category: 'compute', tag: 'union',
    // one input port that accepts MANY edges — stack N datasets vertically
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'], multi: true }],
    outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false,
    blurb: 'stack datasets row-wise (append) — UNION [ALL] BY NAME',
    defaultData: () => ({ title: 'union', status: 'draft', config: { mode: 'all', align: 'name' }, meta: 'union all · by name' }),
  },
  Union,
)
