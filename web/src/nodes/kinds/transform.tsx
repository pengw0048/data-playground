import { useRef, useState } from 'react'
import { register, type NodeComponentProps } from '../registry'
import { NodeCard } from '../NodeCard'
import { useStore } from '../../store/graph'
import { color } from '../../theme/tokens'
import { Segmented, Chip, MiniSelect } from '../../ui/controls'
import { Icon } from '../../ui/Icon'
import { Popover } from '../../ui/Popover'
import { CodeSnippet } from '../../ui/CodeSnippet'
import type { ProcessorMode, TransformSource } from '../../types/graph'

const DEFAULT_CODE = `def fn(row):
    # edit me — runs per row on the sample
    return row`

function Transform({ id, data }: NodeComponentProps) {
  const updateConfig = useStore((s) => s.updateConfig)
  const togglePanel = useStore((s) => s.togglePanel)
  const processors = useStore((s) => s.processors)
  const [pickOpen, setPickOpen] = useState(false)
  const pickRef = useRef<HTMLButtonElement>(null)

  const src: TransformSource = (data.config.source as TransformSource) ?? 'adhoc'
  const mode: ProcessorMode = (data.config.mode as ProcessorMode) ?? 'map'
  const proc = processors.find((p) => p.id === data.config.processor)

  const meta = src === 'library'
    ? (proc ? `${proc.mode} · ${proc.title} · ${proc.version}` : 'pick a processor →')
    : `${mode} · scratch cell`

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
          {src === 'adhoc' && <Chip tone="blue">SCRATCH</Chip>}
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
          <div>
            <button
              onClick={(e) => { e.stopPropagation(); togglePanel(id, 'code') }}
              style={{
                display: 'block', width: '100%', textAlign: 'left', background: 'var(--code-bg)',
                border: `1px solid ${color.border}`, borderRadius: 8, padding: '8px 10px', fontSize: 10.5, lineHeight: 1.4,
                whiteSpace: 'pre', overflow: 'hidden', textOverflow: 'ellipsis', cursor: 'text',
              }}
            >
              <CodeSnippet code={String(data.config.code ?? DEFAULT_CODE).split('\n').slice(0, 3).join('\n')} language="python" />
            </button>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
              <MiniSelect<ProcessorMode>
                value={mode}
                onChange={(v) => updateConfig(id, { mode: v })}
                options={[
                  { value: 'map', label: 'map' },
                  { value: 'map_batches', label: 'map_batches' },
                  { value: 'filter', label: 'filter' },
                  { value: 'flat_map', label: 'flat_map' },
                ]}
              />
              <PromoteButton id={id} />
            </div>
          </div>
        )}
      </div>
    </NodeCard>
  )
}

function PromoteButton({ id }: { id: string }) {
  const promote = useStore((s) => s.promote)
  const [busy, setBusy] = useState(false)
  return (
    <button
      onClick={async (e) => { e.stopPropagation(); setBusy(true); try { await promote(id) } finally { setBusy(false) } }}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap', padding: '5px 9px',
        border: `1px solid ${color.border}`, borderRadius: 7, background: '#fff', color: color.focus, fontSize: 11, fontWeight: 600,
      }}
    >
      {busy ? 'promoting…' : 'Promote to library'} <Icon name="external" size={12} />
    </button>
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
