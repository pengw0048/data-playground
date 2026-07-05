import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'
import { CodeSnippet } from '../../ui/CodeSnippet'

const DEFAULT_SQL = 'SELECT * FROM input LIMIT 100'

function Sql({ id, data }: NodeComponentProps) {
  const openFullscreen = useStore((s) => s.openCodeFullscreen)
  const sql = String(data.config.sql ?? DEFAULT_SQL)
  return (
    <NodeCard id={id} data={data} metaOverride="SQL → view">
      <button
        onClick={(e) => { e.stopPropagation(); openFullscreen(id, 'sql', 'sql') }}
        style={{
          display: 'block', width: '100%', textAlign: 'left', background: 'var(--code-bg)',
          border: `1px solid ${color.border}`, borderRadius: 8, padding: '8px 10px', fontSize: 10.5, lineHeight: 1.4,
          whiteSpace: 'pre-wrap', cursor: 'text', maxHeight: 54, overflow: 'hidden',
        }}
      >
        <CodeSnippet code={sql} language="sql" />
      </button>
    </NodeCard>
  )
}

register(
  {
    kind: 'sql',
    title: 'sql',
    category: 'query',
    tag: 'sql',
    // accepts matches the backend spec (nodespecs.py `sql`): dataset + sample. No node emits a
    // 'sql-view' wire, so accepting it here only let frontend canConnect diverge from backend validation.
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'SQL over a table → a queryable view',
    defaultData: () => ({ title: 'sql', status: 'draft', config: { sql: DEFAULT_SQL }, meta: 'SQL → view' }),
  },
  Sql,
)
