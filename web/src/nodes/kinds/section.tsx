import { useEffect, useState } from 'react'
import { useUpdateNodeInternals } from '@xyflow/react'
import { register, nodeOutputs, type NodeComponentProps } from '../registry'
import { Port } from '../Port'
import { useStore, nodeRunnable } from '../../store/graph'
import { status as statusTok } from '../../theme/tokens'
import { Icon } from '../../ui/Icon'
import { Tooltip } from '../../ui/Tooltip'
import { cn } from '@/lib/utils'

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

      <div className={cn(
        'flex h-full w-full flex-col rounded-lg border-[1.5px] bg-muted/40',
        selected ? 'border-solid border-primary ring-[3px] ring-primary/15' : 'border-dashed border-muted-foreground/40',
      )}>
        {/* header */}
        <div className="flex items-center gap-[7px] rounded-t-lg border-b border-border bg-card px-2.5 py-2">
          <span className="w-3 text-center text-xs" style={{ color: st.color }} title={st.label}>{st.glyph}</span>
          {editing ? (
            <input
              autoFocus value={val} onChange={(e) => setVal(e.target.value)} onClick={(e) => e.stopPropagation()}
              onBlur={() => { setEditing(false); if (val.trim()) rename(id, val.trim()) }}
              onKeyDown={(e) => { if (e.key === 'Enter') { setEditing(false); if (val.trim()) rename(id, val.trim()) } if (e.key === 'Escape') { setVal(data.title); setEditing(false) } }}
              className="w-[140px] rounded border border-primary px-1 py-px text-[13px] font-semibold text-foreground outline-none"
            />
          ) : (
            <span onDoubleClick={(e) => { e.stopPropagation(); setEditing(true) }}
              className="flex-1 cursor-text truncate text-[13px] font-semibold text-foreground">
              {data.title}
            </span>
          )}
          <span className={editing ? 'flex-1' : undefined} />
          <span className="rounded bg-muted px-1.5 py-0.5 text-[8.5px] font-semibold tracking-[0.6px] text-muted-foreground">SECTION</span>
          <Tooltip label={runnable ? 'Run up to here' : 'Connect a source to run'}>
            <button aria-label="Run section" aria-disabled={!runnable} onClick={(e) => { e.stopPropagation(); if (runnable) requestRun(id) }}
              className={cn('grid h-[22px] w-6 place-items-center rounded-md', runnable ? 'cursor-pointer text-muted-foreground' : 'cursor-not-allowed text-muted-foreground/40')}>
              <Icon name="play" size={13} />
            </button>
          </Tooltip>
        </div>

        {/* body — the drop zone for contained nodes */}
        <div className="flex flex-1 flex-col items-center justify-center gap-2 p-3">
          <div className="text-center text-[11.5px] leading-normal text-muted-foreground">
            {childCount > 0
              ? `${childCount} contained node${childCount > 1 ? 's' : ''} · script drives them`
              : 'Drop nodes here to contain them,\nthen script the loop/branch.'}
          </div>
          <button
            onClick={(e) => { e.stopPropagation(); togglePanel(id, 'section') }}
            className={cn('inline-flex items-center gap-[5px] rounded-md border border-border bg-card px-3 py-[5px] text-[11.5px]', openPanel === 'section' ? 'text-primary' : 'text-muted-foreground')}
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
