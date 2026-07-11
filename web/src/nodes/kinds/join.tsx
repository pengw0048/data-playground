import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { getBackendSpec } from '../generic'
import { useStore } from '../../store/graph'
import { Field, MiniSelect, MiniInput } from '../../ui/controls'
import { ColumnCombo, useInputColumns } from '../fields'
import type { NodeConfig } from '../../types/graph'

function Join({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const on = String(data.config.on ?? '')
  const cond = String(data.config.condition ?? '')
  const how = (data.config.how as NodeConfig['how']) ?? 'inner'
  // derive the join types from the backend NodeSpec (source of truth: inner/left/right/outer) instead of
  // a hardcoded subset, so the card can't silently drift from what the engine supports (UX-05). The
  // fallback covers the brief pre-bootstrap window before backendSpecs is populated.
  const howOptions = ((getBackendSpec('join')?.params.find((p) => p.name === 'how')?.options
    ?? ['inner', 'left', 'right', 'outer']) as NonNullable<NodeConfig['how']>[])
  const columns = useInputColumns(id)  // union of left + right port columns
  return (
    <NodeCard id={id} data={data} metaOverride={`${how}${cond ? ` · on ${cond}` : on ? ` · on ${on}` : ''}`}>
      <div className="flex gap-2">
        <Field label="shared key" style={{ flex: 1.4 }}>
          <ColumnCombo value={on} columns={columns} placeholder="key" onChange={(v) => updateConfig(id, { on: v })} />
        </Field>
        <Field label="how" style={{ flex: 1 }}>
          <MiniSelect value={how} onChange={(v) => updateConfig(id, { how: v })} options={howOptions.map((o) => ({ value: o, label: o }))} />
        </Field>
      </div>
      <Field label="or ON expression" style={{ marginTop: 6 }}>
        <MiniInput value={cond} placeholder="a.user_id = b.uid" onChange={(v) => updateConfig(id, { condition: v })} />
      </Field>
    </NodeCard>
  )
}

register(
  {
    kind: 'join',
    title: 'join',
    category: 'compute',
    tag: 'join',
    inputs: [
      { id: 'a', label: 'left', wire: 'dataset', accepts: ['dataset', 'sample'] },
      { id: 'b', label: 'right', wire: 'dataset', accepts: ['dataset', 'sample'] },
    ],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'combine two datasets on a key',
    defaultData: () => ({ title: 'join', status: 'draft', config: { how: 'inner', on: '' }, meta: 'inner' }),
  },
  Join,
)
