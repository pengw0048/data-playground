import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { Field, MiniInput } from '../../ui/controls'

function VectorSearch({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const col = String(data.config.column ?? 'embedding')
  const k = Number(data.config.k ?? 10)
  const qrow = Number(data.config.queryRow ?? 0)
  const hasVec = Array.isArray(data.config.queryVector) && (data.config.queryVector as unknown[]).length > 0
  return (
    <NodeCard id={id} data={data} metaOverride={`top-${k} by cosine · ${col}${hasVec ? ' · query vector' : ` · row ${qrow}`}`}>
      <div style={{ display: 'flex', gap: 8 }}>
        <Field label="vector column" style={{ flex: 1.5 }}>
          <MiniInput mono value={col} onChange={(v) => updateConfig(id, { column: v })} />
        </Field>
        <Field label="k" style={{ flex: 0.8 }}>
          <MiniInput value={String(k)} onChange={(v) => updateConfig(id, { k: Number(v) || 1 })} />
        </Field>
        <Field label="query row" style={{ flex: 0.9 }}>
          <MiniInput value={String(qrow)} onChange={(v) => updateConfig(id, { queryRow: Number(v) || 0 })} />
        </Field>
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'vector-search', title: 'vector-search', category: 'query', tag: 'vector',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset'] }],
    outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false,
    blurb: 'top-K nearest by cosine similarity',
    defaultData: () => ({ title: 'vector-search', status: 'draft', config: { column: 'embedding', k: 10 }, meta: 'top-K nearest' }),
  },
  VectorSearch,
)
