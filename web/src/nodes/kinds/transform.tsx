import { useRef, useState } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'
import { Segmented } from '../../ui/controls'
import { Icon } from '../../ui/Icon'
import { Popover } from '../../ui/Popover'
import { CodeSnippet } from '../../ui/CodeSnippet'
import { cn } from '@/lib/utils'
import type { ProcessorMode, TransformSource } from '../../types/graph'

const DEFAULT_CODE = `def fn(row):
    # edit me — runs per row
    return row`

// The single Python-code compute node (the old `notebook` folded in here). `scope` just labels
// whether you're exploring a sample or producing a dataset — execution is identical. Code is
// edited in the one fullscreen editor (mode / on_error / Promote live there too).
function Transform({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const openFullscreen = useStore((s) => s.openCodeFullscreen)
  const processors = useStore((s) => s.processors)
  const [pickOpen, setPickOpen] = useState(false)
  const pickRef = useRef<HTMLButtonElement>(null)

  const src: TransformSource = (data.config.source as TransformSource) ?? 'adhoc'
  const mode: ProcessorMode = (data.config.mode as ProcessorMode) ?? 'map'
  const scope = (data.config.scope as 'dataset' | 'sample') ?? 'dataset'
  const proc = processors.find((p) => p.id === data.config.processor)

  const meta = src === 'library'
    ? (proc ? `${proc.mode} · ${proc.title} · ${proc.version}` : 'pick a processor →')
    : `${mode} · on ${scope}`

  return (
    <NodeCard id={id} data={data} metaOverride={meta}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {/* two segmented toggles — wrap (not clip) on the narrow card; the scope drops to its own row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <Segmented<TransformSource>
            options={[{ value: 'library', label: 'Library' }, { value: 'adhoc', label: 'Ad-hoc' }]}
            value={src}
            accent={src === 'adhoc' ? color.focus : '#2f9e8f'}
            onChange={(v) => updateConfig(id, {
              source: v,
              code: v === 'adhoc' ? (data.config.code ?? DEFAULT_CODE) : data.config.code,
            })}
          />
          {src === 'adhoc' && (
            <Segmented<'dataset' | 'sample'>
              options={[{ value: 'dataset', label: 'dataset' }, { value: 'sample', label: 'sample' }]}
              value={scope} accent="#8a6d0b"
              onChange={(v) => updateConfig(id, { scope: v })}
            />
          )}
        </div>

        {src === 'library' ? (
          <div>
            <button
              ref={pickRef}
              onClick={(e) => { e.stopPropagation(); setPickOpen((v) => !v) }}
              className={cn(
                'flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-[11.5px]',
                proc ? 'text-foreground' : 'text-muted-foreground',
              )}
            >
              <Icon name="fx" size={13} />
              <span className="flex-1 truncate text-left">
                {proc?.title ?? 'select processor'}
              </span>
              {proc && <span className="text-[10px] text-muted-foreground">{proc.version}</span>}
              <Icon name="chevronDown" size={12} />
            </button>
            <Popover anchorRef={pickRef} open={pickOpen} onClose={() => setPickOpen(false)} width={220}>
              {processors.length === 0 && (
                <div className="p-[9px] text-[11px] leading-[1.4] text-muted-foreground">
                  Library is empty. Write an ad-hoc cell and “Promote to library”.
                </div>
              )}
              {processors.map((p) => (
                <button
                  key={p.id}
                  onClick={(e) => { e.stopPropagation(); updateConfig(id, { processor: p.id, version: p.version, mode: p.mode }); setPickOpen(false) }}
                  className="flex w-full flex-col gap-px rounded-md px-[9px] py-[7px] text-left hover:bg-accent"
                >
                  <span className="text-xs font-semibold text-foreground">{p.title} <span className="font-normal text-muted-foreground">· {p.mode}</span></span>
                </button>
              ))}
            </Popover>
          </div>
        ) : (
          <button
            onClick={(e) => { e.stopPropagation(); openFullscreen(id, 'code', 'python') }}
            title="Open the code editor"
            className="block w-full cursor-text overflow-hidden text-ellipsis whitespace-pre rounded-md border border-border bg-[var(--code-bg)] px-2.5 py-2 text-left text-[10.5px] leading-[1.4]"
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
