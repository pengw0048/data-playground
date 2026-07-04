import { useEffect, useRef, useState, type ReactNode } from 'react'
import { color, kindAccent, radius, shadow, status as statusTok } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Tooltip } from '../ui/Tooltip'
import { Popover } from '../ui/Popover'
import { Port } from './Port'
import { getSpec, type NodeSpec } from './registry'
import { useStore, nodeRunnable, type PanelKind } from '../store/graph'
import { exportNode } from '../lib/exporters'
import type { NodeData } from '../types/graph'

const KINDS_WITH_CODE = new Set(['transform', 'sql', 'notebook'])

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
  const rename = useStore((s) => s.rename)
  const runState = useStore((s) => s.runs[id]?.phase)
  const runnable = useStore((s) => nodeRunnable(s.doc, id))

  const kind = node?.type ?? 'transform'
  const accent = kindAccent[kind] ?? '#8a8f98'
  const st = statusTok[data.status] ?? statusTok.draft
  const bypassed = !!data.bypassed
  const muted = !!data.muted
  const hasCode = KINDS_WITH_CODE.has(kind)

  const tag = (spec?.tag ?? kind).toUpperCase()

  const border = bypassed
    ? `1.5px dashed ${accent}`
    : selected
      ? `1px solid ${color.focus}`
      : `1px solid ${color.border}`

  return (
    <div style={{ position: 'relative', width: 232, opacity: muted ? 0.5 : 1 }} className="dp-no-select">
      {/* input ports */}
      {(spec?.inputs ?? []).map((p, i) => (
        <Port key={p.id} spec={p} side="input" index={i} count={spec!.inputs.length} />
      ))}
      {/* output ports */}
      {(spec?.outputs ?? []).map((p, i) => (
        <Port key={p.id} spec={p} side="output" index={i} count={spec!.outputs.length} nodeId={id} />
      ))}

      <div
        style={{
          background: color.card,
          border,
          borderRadius: radius.node,
          boxShadow: selected ? shadow.focus : shadow.card,
          overflow: 'hidden',
          filter: muted ? 'grayscale(0.6)' : undefined,
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

            {/* compact body (kind-specific, kept small — P5) */}
            {children && <div style={{ marginTop: 8 }}>{children}</div>}

            {/* action shelf */}
            <div
              style={{
                display: 'flex', alignItems: 'center', gap: 2, marginTop: 10,
                marginLeft: -10, marginRight: -12, padding: '5px 8px',
                borderTop: `1px solid ${color.hairline}`, background: '#f7f8fa',
              }}
            >
              <ActionIcon
                name="eye" label={runnable ? 'View data' : 'Connect a source to preview'}
                active={openPanel === 'data'} disabled={!runnable}
                onClick={() => runPreview(id)}
              />
              <ActionIcon
                name={runState === 'running' ? 'stop' : 'play'}
                label={!runnable ? 'Connect a source to run' : runState === 'running' ? 'Stop' : 'Run up to here'}
                active={openPanel === 'run'}
                disabled={!runnable}
                // ▶ always runs THIS node (up to & including it); ⏹ cancels. Run details live in the ⋯ menu.
                onClick={() => (runState === 'running' ? cancelRun(id) : requestRun(id))}
              />
              <ActionIcon name="clock" label="History" active={openPanel === 'history'} onClick={() => togglePanel(id, 'history')} />
              {hasCode && <ActionIcon name="code" label="Code" active={openPanel === 'code'} onClick={() => togglePanel(id, 'code')} />}
              <span style={{ flex: 1 }} />
              <MoreMenu id={id} kind={kind} />
            </div>
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
  const { bypass, mute, duplicate, removeNode, openPanel } = useStore.getState()
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
        {canBypass && item('power', 'Bypass', () => bypass(id))}
        {item('mute', 'Mute', () => mute(id))}
        {item('export', 'Export data', () => exportNode(id))}
        {item('lineage', 'Lineage', () => openPanel(id, 'lineage'))}
        <div style={{ height: 1, background: color.hairline, margin: '4px 0' }} />
        {item('trash', 'Delete', () => removeNode(id), true)}
      </Popover>
    </>
  )
}
