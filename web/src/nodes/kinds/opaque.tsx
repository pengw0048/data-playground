import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { color, radius } from '../../theme/tokens'
import { Icon } from '../../ui/Icon'

function Opaque({ id, data }: NodeComponentProps) {
  return (
    <NodeCard id={id} data={data} metaOverride={`${String(data.config.mode ?? 'callable')} · opaque driver logic`}>
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 7, padding: '7px 9px',
          background: '#fbf6e9', border: `1px solid #ecdcb4`, borderRadius: radius.button,
          color: '#95701d', fontSize: 11,
        }}
      >
        <Icon name="power" size={13} />
        Not sample-previewable — needs a full pass
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'opaque',
    title: 'opaque',
    category: 'control',
    tag: 'opaque',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'imported driver logic that does not decompose',
    defaultData: () => ({ title: 'opaque', status: 'draft', config: { mode: 'callable' }, meta: 'needs full pass', needsFullPass: true }),
  },
  Opaque,
)
