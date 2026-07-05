import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { CodeSnippet } from '../../ui/CodeSnippet'

const DEFAULT_SQL = 'SELECT * FROM input LIMIT 100'

function Sql({ id, data }: NodeComponentProps) {
  const openFullscreen = useStore((s) => s.openCodeFullscreen)
  const sql = String(data.config.sql ?? DEFAULT_SQL)
  return (
    <NodeCard id={id} data={data} metaOverride="SQL → view">
      <button
        onClick={(e) => { e.stopPropagation(); openFullscreen(id, 'sql', 'sql') }}
        className="block max-h-[54px] w-full cursor-text overflow-hidden whitespace-pre-wrap rounded-md border border-border bg-[var(--code-bg)] px-2.5 py-2 text-left text-[10.5px] leading-[1.4]"
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
