import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'
import { Segmented } from '../../ui/controls'
import { Icon } from '../../ui/Icon'
import { CodeSnippet } from '../../ui/CodeSnippet'
import { cn } from '@/lib/utils'
import type { ProcessorMode, TransformSource } from '../../types/graph'
import { configuredProcessorRef, exactProcessor } from '../processorIdentity'

const DEFAULT_CODE = `def fn(row):
    # edit me — runs per row
    return row`

// The single Python-code compute node. `scope` labels whether you're exploring a sample or producing
// a dataset — execution is identical. Code is edited in the one fullscreen editor (mode / on_error /
// Promote live there too).
function Transform({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const openFullscreen = useStore((s) => s.openCodeFullscreen)
  const processors = useStore((s) => s.processors)
  const references = useStore((s) => s.canvasTransformReferences)
  const setTransformResource = useStore((s) => s.setTransformResource)
  const canvasId = useStore((s) => s.doc.id)

  const src: TransformSource = (data.config.source as TransformSource) ?? 'adhoc'
  const mode: ProcessorMode = (data.config.mode as ProcessorMode) ?? 'map'
  const scope = (data.config.scope as 'dataset' | 'sample') ?? 'dataset'
  const reference = references.find((candidate) => (
    candidate.id === data.config.processor && candidate.version === data.config.version
  ))
  const proc = exactProcessor(processors, data.config.processor, data.config.version)
    ?? reference?.descriptor ?? undefined
  const configuredRef = configuredProcessorRef(
    data.config.processor, data.config.version)

  const meta = src === 'library'
    ? (reference?.availability === 'deleted' ? `${configuredRef} · deleted`
      : reference?.availability === 'missing' ? `${configuredRef} · unavailable`
      : proc ? `${proc.mode} · ${proc.title} · ${proc.version}` : (configuredRef ?? 'choose in Transforms →'))
    : `${mode} · on ${scope}`

  return (
    <NodeCard id={id} data={data} metaOverride={meta}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {/* two segmented toggles — wrap (not clip) on the narrow card; the scope drops to its own row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <Segmented<TransformSource>
            options={[{ value: 'library', label: 'Library' }, { value: 'adhoc', label: 'Ad-hoc' }]}
            value={src}
            accent={color.focus}
            onChange={(v) => updateConfig(id, {
              source: v,
              code: v === 'adhoc' ? (data.config.code ?? DEFAULT_CODE) : data.config.code,
            })}
          />
          {src === 'adhoc' && (
            <Segmented<'dataset' | 'sample'>
              options={[{ value: 'dataset', label: 'dataset' }, { value: 'sample', label: 'sample' }]}
              value={scope} accent={color.focus}
              onChange={(v) => updateConfig(id, { scope: v })}
            />
          )}
        </div>

        {src === 'library' ? (
          <div>
            <button
              onClick={(e) => {
                e.stopPropagation()
                setTransformResource(
                  typeof data.config.processor === 'string' ? data.config.processor : null,
                  typeof data.config.version === 'string' ? data.config.version : null,
                  typeof data.config.processor === 'string'
                    ? { canvasId, nodeId: id } : null,
                )
              }}
              className={cn(
                'flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px]',
                proc ? 'text-foreground' : 'text-muted-foreground',
              )}
            >
              <Icon name="fx" size={13} />
              <span className="flex-1 truncate text-left">
                {proc?.title ?? configuredRef ?? 'select processor'}
              </span>
              {proc && <span className="text-[10px] text-muted-foreground">{proc.version}</span>}
              <span className="text-[10px] font-semibold text-primary">Manage</span>
            </button>
          </div>
        ) : (
          <button
            onClick={(e) => { e.stopPropagation(); openFullscreen(id, 'code', 'python') }}
            title="Open the code editor"
            className="block max-h-[54px] w-full cursor-text overflow-hidden whitespace-pre-wrap rounded-md border border-border bg-[var(--code-bg)] px-2.5 py-2 text-left text-[10.5px] leading-[1.4]"
          >
            <CodeSnippet code={String(data.config.code ?? DEFAULT_CODE).split('\n').slice(0, 3).join('\n')} language="python" />
          </button>
        )}
      </div>
    </NodeCard>
  )
}

register(
  {
    kind: 'transform',
    title: 'transform',
    category: 'compute',
    tag: 'transform',
    inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset', 'sample', 'selection'] }],
    outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: true,
    blurb: 'the operator — library preset or ad-hoc cell',
    defaultData: () => ({
      title: 'transform', status: 'draft',
      config: { source: 'adhoc', mode: 'map', code: DEFAULT_CODE, onError: 'raise' },
      meta: 'map · scratch cell',
    }),
  },
  Transform,
)
