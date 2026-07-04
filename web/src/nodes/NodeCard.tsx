import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useUpdateNodeInternals } from '@xyflow/react'
import { color, kindAccent, radius, shadow, status as statusTok } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Tooltip } from '../ui/Tooltip'
import { Popover } from '../ui/Popover'
import { Port } from './Port'
import { getSpec, nodeOutputs, type NodeSpec } from './registry'
import { nodeInvalidReason } from './generic'
import { useStore, nodeRunnable, isDisabled, type PanelKind } from '../store/graph'
import { exportNode } from '../lib/exporters'
import type { NodeData } from '../types/graph'

const KINDS_WITH_CODE = new Set(['transform', 'sql'])

export function NodeCard({ id, data, children, metaOverride }: {
  id: string
  data: NodeData
  children?: ReactNode
  metaOverride?: ReactNode
}) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === id))
  const spec = getSpec(node?.type ?? 'transform') as NodeSpec | undefined
  const selected = useStore((s) => s.selectedIds.includes(id))
  const openPanel = useStore((s) => s.openPanels[id])
  const runPreview = useStore((s) => s.runPreview)
  const requestRun = useStore((s) => s.requestRun)
  const cancelRun = useStore((s) => s.cancelRun)
  const togglePanel = useStore((s) => s.togglePanel)
  const openCodeFullscreen = useStore((s) => s.openCodeFullscreen)
  const rename = useStore((s) => s.rename)
  const runState = useStore((s) => s.runs[id]?.phase)
  const runnable = useStore((s) => nodeRunnable(s.doc, id))
  const previewed = useStore((s) => !!s.previews[id]?.result && !s.previews[id]?.result?.notPreviewable)
  const [hover, setHover] = useState(false)
  // disabled = this node is turned off; offDownstream = an upstream node is off, so this one is off too
  const offDownstream = useStore((s) => !s.doc.nodes.find((n) => n.id === id)?.data.disabled && isDisabled(s.doc, id))

  // Output ports can change at runtime (a section declaring named ports). React Flow caches each
  // node's handle geometry, so a newly-added handle is invisible to edge routing until we tell it
  // to re-measure — without this, wiring a freshly-declared port silently drops the edge.
  const updateNodeInternals = useUpdateNodeInternals()
  const outSig = (node ? nodeOutputs(node) : []).map((p) => p.id).join(',')
  useEffect(() => { updateNodeInternals(id) }, [id, outSig, updateNodeInternals])

  const kind = node?.type ?? 'transform'
  const accent = kindAccent[kind] ?? '#8a8f98'
  const st = statusTok[data.status] ?? statusTok.draft
  const bypassed = !!data.bypassed
  const disabled = !!data.disabled
  const off = disabled || offDownstream  // dimmed either way; only self-disabled shows the badge
  const hasCode = KINDS_WITH_CODE.has(kind)
  const busy = runState === 'running' || runState === 'estimating'
  const invalid = node ? nodeInvalidReason(node) : null   // e.g. "order by is required"
  // the action shelf is revealed on hover / selection / while running, so a resting card is clean
  const showShelf = selected || hover || busy
  // a preview is a 50-row peek, NOT a full materialized run — show it as "sampled", not latest-green
  const sampled = previewed && data.status !== 'latest' && data.status !== 'running'

  const tag = (spec?.tag ?? kind).toUpperCase()

  const border = bypassed
    ? `1.5px dashed ${accent}`
    : selected
      ? `1px solid ${color.focus}`
      : `1px solid ${color.border}`

  return (
    <div style={{ position: 'relative', width: 232, opacity: off ? 0.45 : 1 }} className="dp-no-select"
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      {/* input ports */}
      {(spec?.inputs ?? []).map((p, i) => (
        <Port key={p.id} spec={p} side="input" index={i} count={spec!.inputs.length} />
      ))}
      {/* output ports — instance-declared (multi-output) or the static spec */}
      {(node ? nodeOutputs(node) : spec?.outputs ?? []).map((p, i, arr) => (
        <Port key={p.id} spec={p} side="output" index={i} count={arr.length} nodeId={id} />
      ))}

      <div
        style={{
          background: color.card,
          border,
          borderRadius: radius.node,
          boxShadow: selected ? shadow.focus : shadow.card,
          overflow: 'hidden',
          filter: off ? 'grayscale(0.7)' : undefined,
          transition: 'box-shadow .12s, border-color .12s',
        }}
      >
        <div style={{ display: 'flex' }}>
          {/* accent stripe */}
          <div style={{ width: 6, background: bypassed ? 'transparent' : accent, flex: '0 0 6px' }} />
          <div style={{ flex: 1, minWidth: 0, padding: '11px 12px 0 10px' }}>
            {/* header */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
              <span
                className={data.status === 'running' ? 'dp-running-glyph' : undefined}
                style={{ color: st.color, fontSize: 12, lineHeight: 1, width: 12, textAlign: 'center' }}
                title={st.label}
              >
                {st.glyph}
              </span>
              <EditableTitle id={id} title={data.title} onRename={rename} selected={selected} />
              <span style={{ flex: 1 }} />
              {sampled && (
                <span title="Showing a sampled preview — not a full run" style={{ fontSize: 8.5, fontWeight: 700, letterSpacing: 0.5, color: '#3355c6', background: '#e7ecfb', padding: '2px 6px', borderRadius: radius.chip, flex: '0 0 auto' }}>
                  SAMPLED
                </span>
              )}
              {disabled && (
                <span style={{ fontSize: 8.5, fontWeight: 700, letterSpacing: 0.5, color: '#8a6d0b', background: '#fbf1dc', padding: '2px 6px', borderRadius: radius.chip, flex: '0 0 auto' }}>
                  DISABLED
                </span>
              )}
              <span
                style={{
                  fontSize: 8.5, fontWeight: 600, letterSpacing: 0.6, color: color.text3,
                  background: '#f1f2f4', padding: '2px 6px', borderRadius: radius.chip, flex: '0 0 auto',
                }}
              >
                {tag}
              </span>
            </div>

            {/* meta */}
            <div style={{ marginTop: 5, fontSize: 11.5, color: color.text2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minHeight: 16 }}>
              {metaOverride ?? data.meta ?? ''}
            </div>

            {/* a run awaiting confirmation stays visible ON the card (so a rerun-all of several
                sinks doesn't hide all-but-one behind the single floating panel) */}
            {runState === 'confirm' && (
              <button className="nodrag" onClick={(e) => { e.stopPropagation(); useStore.getState().openPanel(id, 'run') }}
                style={{ marginTop: 6, display: 'inline-flex', alignItems: 'center', gap: 5, border: '1px solid #e7c66b', background: '#fbf1dc', color: '#8a6d0b', fontSize: 11, fontWeight: 600, padding: '4px 9px', borderRadius: 7, cursor: 'pointer' }}>
                <Icon name="power" size={11} /> Confirm run…
              </button>
            )}

            {/* compact body (kind-specific, kept small — P5) */}
            {children && <div style={{ marginTop: 8 }}>{children}</div>}

            {/* action shelf — revealed on hover / selection / run (a resting card stays clean) */}
            {showShelf && (
              <div
                style={{
                  display: 'flex', alignItems: 'center', gap: 2, marginTop: 10,
                  marginLeft: -10, marginRight: -12, padding: '5px 8px',
                  borderTop: `1px solid ${color.hairline}`, background: '#f7f8fa',
                }}
              >
                <ActionIcon
                  name="eye" label={invalid ?? (runnable ? 'View data' : 'Connect a source to preview')}
                  active={openPanel === 'data'} disabled={!runnable || !!invalid}
                  onClick={() => runPreview(id)}
                />
                {/* a source has no compute — its ▶ (a full COUNT/scan) is deliberately not a quick
                    action here; preview (eye) is. Run/materialize stays available in the Inspector. */}
                {kind !== 'source' && (
                  <ActionIcon
                    name={busy ? 'stop' : 'play'}
                    label={invalid ?? (!runnable ? 'Connect a source to run' : busy ? 'Stop' : 'Run up to here')}
                    active={openPanel === 'run'}
                    disabled={(!runnable || !!invalid) && !busy}
                    // ▶ runs THIS node (up to & including it); ⏹ cancels. Run details live in the ⋯ menu.
                    onClick={() => (busy ? cancelRun(id) : requestRun(id))}
                  />
                )}
                <ActionIcon name="clock" label="History" active={openPanel === 'history'} onClick={() => togglePanel(id, 'history')} />
                {hasCode && <ActionIcon name="code" label="Edit code" onClick={() => openCodeFullscreen(id, kind === 'sql' ? 'sql' : 'code', kind === 'sql' ? 'sql' : 'python')} />}
                <span style={{ flex: 1 }} />
                <MoreMenu id={id} kind={kind} />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function ActionIcon({ name, label, active, onClick, disabled }: {
  name: IconName; label: string; active?: boolean; onClick: () => void; disabled?: boolean
}) {
  const [hover, setHover] = useState(false)
  return (
    <Tooltip label={label}>
      <button
        aria-label={label}
        aria-disabled={disabled}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        onClick={(e) => { e.stopPropagation(); if (!disabled) onClick() }}
        style={{
          width: 26, height: 24, display: 'grid', placeItems: 'center', border: 'none',
          borderRadius: 6,
          background: disabled ? 'transparent' : active ? '#e7ebf5' : hover ? '#eceef1' : 'transparent',
          color: disabled ? '#c8ccd2' : active ? color.focus : hover ? color.ink : color.text3,
          cursor: disabled ? 'not-allowed' : 'pointer',
          transition: 'background .1s, color .1s',
        }}
      >
        <Icon name={name} />
      </button>
    </Tooltip>
  )
}

function EditableTitle({ id, title, onRename, selected }: { id: string; title: string; onRename: (id: string, t: string) => void; selected?: boolean }) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState(title)
  useEffect(() => setVal(title), [title])
  // let the ⋯-menu "Rename" (and any external trigger) enter edit mode for this node
  useEffect(() => {
    const onRenameEvt = (e: Event) => { if ((e as CustomEvent).detail?.id === id) setEditing(true) }
    window.addEventListener('dp-rename', onRenameEvt)
    return () => window.removeEventListener('dp-rename', onRenameEvt)
  }, [id])
  if (editing) {
    return (
      <input
        autoFocus
        value={val}
        onChange={(e) => setVal(e.target.value)}
        onClick={(e) => e.stopPropagation()}
        onBlur={() => { setEditing(false); if (val.trim()) onRename(id, val.trim()) }}
        onKeyDown={(e) => { if (e.key === 'Enter') { setEditing(false); if (val.trim()) onRename(id, val.trim()) } if (e.key === 'Escape') { setVal(title); setEditing(false) } }}
        style={{ fontSize: 13.5, fontWeight: 600, color: color.ink, border: `1px solid ${color.focus}`, borderRadius: 5, padding: '1px 4px', outline: 'none', width: 130 }}
      />
    )
  }
  return (
    <span
      // click the name of an already-selected node to rename (Figma-style); double-click always works
      onClick={(e) => { if (selected) { e.stopPropagation(); setEditing(true) } }}
      onDoubleClick={(e) => { e.stopPropagation(); setEditing(true) }}
      title="Click (when selected) or double-click to rename"
      style={{ fontSize: 13.5, fontWeight: 600, color: color.ink, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', cursor: 'text' }}
    >
      {title}
    </span>
  )
}

function MoreMenu({ id, kind }: { id: string; kind: string }) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const { bypass, disable, duplicate, removeNode, openPanel } = useStore.getState()
  const canBypass = getSpec(kind)?.canBypass

  const item = (icon: IconName, label: string, fn: () => void, danger = false) => (
    <button
      onClick={(e) => { e.stopPropagation(); fn(); setOpen(false) }}
      style={{
        display: 'flex', alignItems: 'center', gap: 9, width: '100%', padding: '7px 10px',
        border: 'none', background: 'transparent', color: danger ? color.failed : color.text2,
        fontSize: 12, textAlign: 'left', borderRadius: 6,
      }}
      onMouseEnter={(e) => (e.currentTarget.style.background = '#f2f3f5')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <Icon name={icon} /> {label}
    </button>
  )

  return (
    <>
      <Tooltip label="More">
        <button
          ref={btnRef}
          aria-label="More"
          onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
          style={{
            width: 26, height: 24, display: 'grid', placeItems: 'center', border: 'none',
            borderRadius: 6, background: open ? '#eceef1' : 'transparent', color: open ? color.ink : color.text3,
          }}
        >
          <Icon name="more" />
        </button>
      </Tooltip>
      <Popover anchorRef={btnRef} open={open} onClose={() => setOpen(false)} width={184} align="right">
        {item('rename', 'Rename', () => window.dispatchEvent(new CustomEvent('dp-rename', { detail: { id } })))}
        {item('play', 'Run details', () => openPanel(id, 'run'))}
        {item('duplicate', 'Duplicate', () => duplicate(id))}
        {canBypass && item('power', 'Bypass (pass data through)', () => bypass(id))}
        {item('mute', 'Disable (+ downstream)', () => disable(id))}
        {item('export', 'Export data', () => exportNode(id))}
        {item('lineage', 'Lineage', () => openPanel(id, 'lineage'))}
        <div style={{ height: 1, background: color.hairline, margin: '4px 0' }} />
        {item('trash', 'Delete', () => removeNode(id), true)}
      </Popover>
    </>
  )
}
