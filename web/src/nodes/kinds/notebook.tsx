import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'

const DEFAULT_CODE = `def fn(row):
    # an embedded cell over the sample
    return row`

function Notebook({ id, data }: NodeComponentProps) {
  const togglePanel = useStore((s) => s.togglePanel)
  return (
    <NodeCard id={id} data={data} metaOverride="cells · run in session · sample → sample">
      <button
        onClick={(e) => { e.stopPropagation(); togglePanel(id, 'code') }}
        className="dp-mono"
        style={{ display: 'block', width: '100%', textAlign: 'left', background: 'var(--code-bg)', color: 'var(--code-text)', border: `1px solid ${color.border}`, borderRadius: 8, padding: '8px 10px', fontSize: 10.5, lineHeight: 1.4, whiteSpace: 'pre', overflow: 'hidden', cursor: 'text' }}
      >
        {String(data.config.code ?? DEFAULT_CODE).split('\n').slice(0, 3).join('\n')}
      </button>
    </NodeCard>
  )
}

register(
  {
    kind: 'notebook',
    title: 'notebook',
    category: 'inspect',
    tag: 'notebook',
    inputs: [{ id: 'in', wire: 'sample', accepts: ['sample', 'dataset'] }],
    outputs: [{ id: 'out', wire: 'sample' }],
    canBypass: true,
    blurb: 'an embedded cell operating on a sample',
    defaultData: () => ({ title: 'notebook', status: 'draft', config: { code: DEFAULT_CODE, mode: 'map' }, meta: 'cells · sample → sample' }),
  },
  Notebook,
)
