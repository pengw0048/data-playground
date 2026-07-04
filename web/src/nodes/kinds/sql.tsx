import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'
import { CodeSnippet } from '../../ui/CodeSnippet'

const DEFAULT_SQL = 'SELECT * FROM input LIMIT 100'

function Sql({ id, data }: NodeComponentProps) {
  const togglePanel = useStore((s) => s.togglePanel)
  const sql = String(data.config.sql ?? DEFAULT_SQL)
  return (
    <NodeCard id={id} data={data} metaOverride="SQL → view · DuckDB on the sample">
      <button
        onClick={(e) => { e.stopPropagation(); togglePanel(id, 'code') }}
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
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample', 'sql-view'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false,
    blurb: 'SQL over a table → a queryable view',
    defaultData: () => ({ title: 'sql', status: 'draft', config: { sql: DEFAULT_SQL }, meta: 'SQL → view' }),
  },
  Sql,
)
