import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'

// The meta-programming node: a driver script over contained nodes (loops/branches). Its body is a
// script + a set of aliased sub-nodes it calls; the editor lives in the Section panel. Runs as a
// single node in the outer DAG; not sample-previewable (full pass only). See docs/meta-programming.zh.md.
const DEFAULT_SCRIPT = "# call contained nodes by alias, with for/while/if\nemit(inputs['in'])"

function Section({ id, data }: NodeComponentProps) {
  const togglePanel = useStore((s) => s.togglePanel)
  const subs = (Array.isArray(data.config.subnodes) ? data.config.subnodes : []) as { alias?: string }[]
  return (
    <NodeCard id={id} data={data} metaOverride={`driver script · ${subs.length} node(s)`}>
      <button
        onClick={(e) => { e.stopPropagation(); togglePanel(id, 'section') }}
        style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%',
          background: 'var(--code-bg)', border: `1px solid ${color.border}`, borderRadius: 8,
          padding: '8px 10px', fontSize: 11.5, color: color.text2, cursor: 'pointer',
        }}
      >
        <span>{subs.length ? subs.map((s) => s.alias).filter(Boolean).join(' · ') : 'empty'}</span>
        <span style={{ color: color.focus, fontWeight: 600 }}>Edit →</span>
      </button>
    </NodeCard>
  )
}

register(
  {
    kind: 'section',
    title: 'section',
    category: 'compute',
    tag: 'section',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'driver script over contained nodes (loops / branches)',
    defaultData: () => ({ title: 'section', status: 'draft', meta: 'driver script',
      config: { script: DEFAULT_SCRIPT, subnodes: [], params: {}, maxRuns: 200, outputs: ['out'] } }),
  },
  Section,
)
