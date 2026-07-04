import { useEffect, useState } from 'react'
import { useUpdateNodeInternals } from '@xyflow/react'
import { register, nodeOutputs, type NodeComponentProps } from '../registry'
import { Port } from '../Port'
import { useStore, nodeRunnable } from '../../store/graph'
import { color, radius, status as statusTok } from '../../theme/tokens'
import { Icon } from '../../ui/Icon'
import { Tooltip } from '../../ui/Tooltip'

// The meta-programming node renders as a CONTAINER frame: nodes dropped inside it (parentId) are the
// section's callable children (alias = their title); a driver script (in the panel) calls them with
// for/while/if. It runs as one node in the outer DAG; not sample-previewable. See docs/meta-programming.zh.md.
export const SECTION_W = 360
export const SECTION_H = 240
const DEFAULT_SCRIPT = "# call contained nodes by title, with for/while/if\nemit(inputs['in'])"

function Section({ id, data, selected }: NodeComponentProps) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === id))
  const childCount = useStore((s) => s.doc.nodes.filter((n) => n.parentId === id).length)
  const togglePanel = useStore((s) => s.togglePanel)
  const requestRun = useStore((s) => s.requestRun)
  const rename = useStore((s) => s.rename)
  const openPanel = useStore((s) => s.openPanels[id])
  const runnable = useStore((s) => nodeRunnable(s.doc, id))
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState(data.title)
  useEffect(() => setVal(data.title), [data.title])

  // output ports can change at runtime (declared emit ports) — re-measure so edges route (see NodeCard)
  const updateNodeInternals = useUpdateNodeInternals()
  const outs = node ? nodeOutputs(node) : []
  const outSig = outs.map((p) => p.id).join(',')
  useEffect(() => { updateNodeInternals(id) }, [id, outSig, updateNodeInternals])

  const st = statusTok[data.status] ?? statusTok.draft
  const inputs = [{ id: 'in', wire: 'dataset' as const, accepts: ['dataset', 'sample'] as const }]

  return (
    <div style={{ position: 'relative', width: SECTION_W, height: SECTION_H }} className="dp-no-select">
      {inputs.map((p, i) => <Port key={p.id} spec={p as any} side="input" index={i} count={inputs.length} />)}
      {outs.map((p, i) => <Port key={p.id} spec={p} side="output" index={i} count={outs.length} nodeId={id} />)}

      <div style={{
        width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
        background: 'rgba(138,143,152,0.05)', border: `1.5px ${selected ? 'solid' : 'dashed'} ${selected ? color.focus : '#b9bec6'}`,
        borderRadius: radius.node, boxShadow: selected ? '0 0 0 3px rgba(60,110,240,0.12)' : undefined,
      }}>
        {/* header */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: 7, padding: '8px 10px',
          borderBottom: `1px solid ${color.hairline}`, background: '#fff',
          borderRadius: `${radius.node}px ${radius.node}px 0 0`,
        }}>
          <span style={{ color: st.color, fontSize: 12, width: 12, textAlign: 'center' }} title={st.label}>{st.glyph}</span>
          {editing ? (
            <input
              autoFocus value={val} onChange={(e) => setVal(e.target.value)} onClick={(e) => e.stopPropagation()}
              onBlur={() => { setEditing(false); if (val.trim()) rename(id, val.trim()) }}
              onKeyDown={(e) => { if (e.key === 'Enter') { setEditing(false); if (val.trim()) rename(id, val.trim()) } if (e.key === 'Escape') { setVal(data.title); setEditing(false) } }}
              style={{ fontSize: 13, fontWeight: 600, color: color.ink, border: `1px solid ${color.focus}`, borderRadius: 5, padding: '1px 4px', outline: 'none', width: 140 }}
            />
          ) : (
            <span onDoubleClick={(e) => { e.stopPropagation(); setEditing(true) }}
              style={{ fontSize: 13, fontWeight: 600, color: color.ink, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'text' }}>
              {data.title}
            </span>
          )}
          <span style={{ flex: editing ? 1 : undefined }} />
          <span style={{ fontSize: 8.5, fontWeight: 600, letterSpacing: 0.6, color: color.text3, background: '#f1f2f4', padding: '2px 6px', borderRadius: radius.chip }}>SECTION</span>
          <Tooltip label={runnable ? 'Run up to here' : 'Connect a source to run'}>
            <button aria-label="Run section" aria-disabled={!runnable} onClick={(e) => { e.stopPropagation(); if (runnable) requestRun(id) }}
              style={{ width: 24, height: 22, display: 'grid', placeItems: 'center', border: 'none', borderRadius: 6, background: 'transparent', color: runnable ? color.text2 : '#c8ccd2', cursor: runnable ? 'pointer' : 'not-allowed' }}>
              <Icon name="play" size={13} />
            </button>
          </Tooltip>
        </div>

        {/* body — the drop zone for contained nodes */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8, padding: 12 }}>
          <div style={{ fontSize: 11.5, color: color.text3, textAlign: 'center', lineHeight: 1.5 }}>
            {childCount > 0
              ? `${childCount} contained node${childCount > 1 ? 's' : ''} · script drives them`
              : 'Drop nodes here to contain them,\nthen script the loop/branch.'}
          </div>
          <button
            onClick={(e) => { e.stopPropagation(); togglePanel(id, 'section') }}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 5, background: '#fff', border: `1px solid ${color.border}`, borderRadius: 8, padding: '5px 12px', fontSize: 11.5, color: openPanel === 'section' ? color.focus : color.text2, cursor: 'pointer' }}
          >
            <Icon name="code" size={12} /> Edit script →
          </button>
        </div>
      </div>
    </div>
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
