import { useRef, useState } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'
import { Segmented } from '../../ui/controls'
import { Icon } from '../../ui/Icon'
import { Popover } from '../../ui/Popover'
import { CodeSnippet } from '../../ui/CodeSnippet'
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
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <Segmented<TransformSource>
            options={[{ value: 'library', label: 'Library' }, { value: 'adhoc', label: 'Ad-hoc code' }]}
            value={src}
            accent={src === 'adhoc' ? color.focus : '#2f9e8f'}
            onChange={(v) => updateConfig(id, {
              source: v,
              code: v === 'adhoc' ? (data.config.code ?? DEFAULT_CODE) : data.config.code,
            })}
          />
          <span style={{ flex: 1 }} />
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
              style={{
                display: 'flex', alignItems: 'center', gap: 6, width: '100%', padding: '6px 8px',
                border: `1px solid ${color.border}`, borderRadius: 7, background: '#fff',
                color: proc ? color.ink : color.text3, fontSize: 11.5,
              }}
            >
              <Icon name="fx" size={13} />
              <span style={{ flex: 1, textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {proc?.title ?? 'select processor'}
              </span>
              {proc && <span style={{ fontSize: 10, color: color.text3 }}>{proc.version}</span>}
              <Icon name="chevronDown" size={12} />
            </button>
            <Popover anchorRef={pickRef} open={pickOpen} onClose={() => setPickOpen(false)} width={220}>
              {processors.length === 0 && (
                <div style={{ padding: 9, fontSize: 11, color: color.text3, lineHeight: 1.4 }}>
                  Library is empty. Write an ad-hoc cell and “Promote to library”.
                </div>
              )}
              {processors.map((p) => (
                <button
                  key={p.id}
                  onClick={(e) => { e.stopPropagation(); updateConfig(id, { processor: p.id, version: p.version, mode: p.mode }); setPickOpen(false) }}
                  style={{ display: 'flex', flexDirection: 'column', gap: 1, width: '100%', textAlign: 'left', padding: '7px 9px', border: 'none', background: 'transparent', borderRadius: 7 }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = '#f2f3f5')}
                  onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                >
                  <span style={{ fontSize: 12, fontWeight: 600, color: color.ink }}>{p.title} <span style={{ color: color.text3, fontWeight: 400 }}>· {p.mode}</span></span>
                </button>
              ))}
            </Popover>
          </div>
        ) : (
          <button
            onClick={(e) => { e.stopPropagation(); openFullscreen(id, 'code', 'python') }}
            title="Open the code editor"
            style={{
              display: 'block', width: '100%', textAlign: 'left', background: 'var(--code-bg)',
              border: `1px solid ${color.border}`, borderRadius: 8, padding: '8px 10px', fontSize: 10.5, lineHeight: 1.4,
              whiteSpace: 'pre', overflow: 'hidden', textOverflow: 'ellipsis', cursor: 'text',
            }}
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
