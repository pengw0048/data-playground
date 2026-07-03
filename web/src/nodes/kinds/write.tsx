import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput, MiniSelect } from '../../ui/controls'

function Write({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const name = String(data.config.name ?? '')
  const mode = (data.config.writeMode as 'append' | 'merge' | 'overwrite') ?? 'overwrite'
  return (
    <NodeCard id={id} data={data} metaOverride={name ? `→ ${name} · ${mode}` : 'name a durable dataset →'}>
      <div style={{ display: 'flex', gap: 8 }}>
        <Field label="name" style={{ flex: 1.6 }}>
          <MiniInput value={name} placeholder="output_table" onChange={(v) => updateConfig(id, { name: v })} />
        </Field>
        <Field label="mode" style={{ flex: 1 }}>
          <MiniSelect value={mode} onChange={(v) => updateConfig(id, { writeMode: v })} options={[{ value: 'overwrite', label: 'overwrite' }, { value: 'append', label: 'append' }, { value: 'merge', label: 'merge' }]} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'write',
    title: 'write',
    category: 'io',
    tag: 'write',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample', 'selection'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'materialize / commit to a registered dataset',
    defaultData: () => ({ title: 'write', status: 'draft', config: { writeMode: 'overwrite' }, meta: 'sink · needs full pass', needsFullPass: true }),
  },
  Write,
)
