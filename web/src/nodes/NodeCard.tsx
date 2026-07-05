import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useUpdateNodeInternals } from '@xyflow/react'
import { kindAccent, status as statusTok } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Tooltip } from '../ui/Tooltip'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
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
  // the action shelf carries SINGLE-node actions, so only show it for a lone selection — a marquee/
  // shift-select of many cards must not float (and strand) one shelf per card
  const soleSelected = useStore((s) => s.selectedIds.length <= 1 && s.selectedIds.includes(id))
  const openPanel = useStore((s) => s.openPanels[id])
  const runPreview = useStore((s) => s.runPreview)
  const requestRun = useStore((s) => s.requestRun)
  const cancelRun = useStore((s) => s.cancelRun)
  const togglePanel = useStore((s) => s.togglePanel)
  const closePanel = useStore((s) => s.closePanel)
  const openCodeFullscreen = useStore((s) => s.openCodeFullscreen)
  const rename = useStore((s) => s.rename)
  const runState = useStore((s) => s.runs[id]?.phase)
  const runnable = useStore((s) => nodeRunnable(s.doc, id))
  // hover drives the action shelf. The shelf is a DOM descendant of this wrapper (just positioned
  // below it), so the wrapper's own enter/leave already covers card↔shelf travel — moving between
  // them never leaves the subtree, so onMouseLeave doesn't fire. A short grace delay on leave then
  // debounces the final exit so a quick brush-past doesn't flicker the shelf.
  const [hover, setHover] = useState(false)
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const enterHover = () => { if (hoverTimer.current) clearTimeout(hoverTimer.current); setHover(true) }
  const leaveHover = () => { hoverTimer.current = setTimeout(() => setHover(false), 160) }
  useEffect(() => () => { if (hoverTimer.current) clearTimeout(hoverTimer.current) }, [])
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
  // the action shelf is revealed on hover / sole-selection / while running, so a resting card is clean
  // and a multi-card marquee doesn't strand a shelf under every selected node
  const showShelf = soleSelected || hover || busy

  const tag = (spec?.tag ?? kind).toUpperCase()

  return (
    <div className={cn('dp-no-select relative w-[232px]', off && 'opacity-45')}
      onMouseEnter={enterHover} onMouseLeave={leaveHover}>
      {/* input ports */}
      {(spec?.inputs ?? []).map((p, i) => (
        <Port key={p.id} spec={p} side="input" index={i} count={spec!.inputs.length} nodeId={id} />
      ))}
      {/* output ports — instance-declared (multi-output) or the static spec */}
      {(node ? nodeOutputs(node) : spec?.outputs ?? []).map((p, i, arr) => (
        <Port key={p.id} spec={p} side="output" index={i} count={arr.length} nodeId={id} />
      ))}

      <div
        // flat card: thin token border, soft shadow. Selection reads as a primary ring (no heavy
        // border); a bypassed node keeps its dashed accent outline (dynamic color → inline).
        className={cn(
          'overflow-hidden rounded-xl border bg-card shadow-sm transition-[box-shadow,border-color] duration-100',
          !bypassed && (selected ? 'border-primary' : 'border-border'),
          selected && 'ring-2 ring-primary/20',
        )}
        style={{
          ...(bypassed ? { border: `1.5px dashed ${accent}` } : {}),
          ...(off ? { filter: 'grayscale(0.7)' } : {}),
        }}
      >
        <div className="flex">
          {/* accent stripe (kind color → inline; tokens can't express per-node values) */}
          <div className="w-1.5 shrink-0" style={{ background: bypassed ? 'transparent' : accent }} />
          <div className="min-w-0 flex-1 pt-[11px] pr-3 pl-2.5">
            {/* header */}
            <div className="flex items-center gap-[7px]">
              <span
                className={cn('w-3 text-center text-xs leading-none', data.status === 'running' && 'dp-running-glyph')}
                style={{ color: st.color }}
                title={st.label}
              >
                {st.glyph}
              </span>
              <EditableTitle id={id} title={data.title} onRename={rename} selected={selected} />
              <span className="flex-1" />
              {disabled && (
                <span className="shrink-0 rounded px-1.5 py-0.5 text-[8.5px] font-bold tracking-[0.5px]"
                  style={{ color: '#8a6d0b', background: '#fbf1dc' }}>
                  DISABLED
                </span>
              )}
              <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[8.5px] font-semibold tracking-[0.6px] text-muted-foreground">
                {tag}
              </span>
            </div>

            {/* meta */}
            <div className="mt-[5px] min-h-4 truncate text-[11.5px] text-muted-foreground">
              {metaOverride ?? data.meta ?? ''}
            </div>

            {/* a run awaiting confirmation stays visible ON the card (so a rerun-all of several
                sinks doesn't hide all-but-one behind the single floating panel) */}
            {runState === 'confirm' && (
              <button className="nodrag mt-1.5 inline-flex cursor-pointer items-center gap-[5px] rounded-md border px-[9px] py-1 text-[11px] font-semibold"
                style={{ borderColor: '#e7c66b', background: '#fbf1dc', color: '#8a6d0b' }}
                onClick={(e) => { e.stopPropagation(); useStore.getState().openPanel(id, 'run') }}>
                <Icon name="power" size={11} /> Confirm run…
              </button>
            )}

            {/* compact body (kind-specific, kept small — P5) */}
            {children && <div className="mt-2">{children}</div>}

          </div>
        </div>
      </div>

      {/* action shelf — revealed on hover / selection / run. It floats BELOW the card (absolute), so
          appearing/disappearing never changes the card's height and the side ports never shift.
          A COMPACT floating toolbar (fit-content) tucked under the card's left edge; a descendant of
          the hover wrapper, so the mouse can travel card ↔ bar without dropping the hover. */}
      {showShelf && (
        <div className="nodrag absolute left-0 top-[calc(100%+5px)] z-[4] inline-flex items-center gap-px rounded-lg border border-border bg-card px-1 py-[3px] shadow-sm">
          <ActionIcon
            name="eye" label={invalid ?? (runnable ? (openPanel === 'data' ? 'Hide data' : 'View data') : 'Connect a source to preview')}
            active={openPanel === 'data'} disabled={!runnable || !!invalid}
            onClick={() => (openPanel === 'data' ? closePanel(id) : runPreview(id))}
          />
          {/* a source has no compute — its ▶ (a full COUNT/scan) is deliberately not a quick action
              here; preview (eye) is. Run/materialize stays available in the Inspector. */}
          {kind !== 'source' && (
            <ActionIcon
              name={busy ? 'stop' : 'play'}
              label={invalid ?? (!runnable ? 'Connect a source to run' : busy ? 'Stop' : 'Run up to here')}
              active={openPanel === 'run'}
              disabled={(!runnable || !!invalid) && !busy}
              onClick={() => (busy ? cancelRun(id) : requestRun(id))}
            />
          )}
          <ActionIcon name="clock" label="History" active={openPanel === 'history'} onClick={() => togglePanel(id, 'history')} />
          {hasCode && <ActionIcon name="code" label="Edit code" onClick={() => openCodeFullscreen(id, kind === 'sql' ? 'sql' : 'code', kind === 'sql' ? 'sql' : 'python')} />}
          <MoreMenu id={id} kind={kind} />
        </div>
      )}
    </div>
  )
}

function ActionIcon({ name, label, active, onClick, disabled }: {
  name: IconName; label: string; active?: boolean; onClick: () => void; disabled?: boolean
}) {
  return (
    <Tooltip label={label}>
      <button
        aria-label={label}
        aria-disabled={disabled}
        onClick={(e) => { e.stopPropagation(); if (!disabled) onClick() }}
        className={cn(
          'grid h-6 w-[26px] place-items-center rounded-md transition-colors',
          disabled
            ? 'cursor-not-allowed bg-transparent text-muted-foreground/40'
            : active
              ? 'bg-primary/10 text-primary'
              : 'cursor-pointer bg-transparent text-muted-foreground hover:bg-accent hover:text-foreground',
        )}
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
        className="w-[130px] rounded-sm border border-primary px-1 py-px text-[13.5px] font-semibold text-foreground outline-none"
      />
    )
  }
  return (
    <span
      // click the name of an already-selected node to rename (Figma-style); double-click always works
      onClick={(e) => { if (selected) { e.stopPropagation(); setEditing(true) } }}
      onDoubleClick={(e) => { e.stopPropagation(); setEditing(true) }}
      title="Click (when selected) or double-click to rename"
      className="cursor-text truncate text-[13.5px] font-semibold text-foreground"
    >
      {title}
    </span>
  )
}

function MoreMenu({ id, kind }: { id: string; kind: string }) {
  const [open, setOpen] = useState(false)
  const { bypass, disable, duplicate, removeNode, openPanel } = useStore.getState()
  const canBypass = getSpec(kind)?.canBypass

  // items call store actions directly (no Dialogs), so onSelect can run inline and let the menu
  // close normally. role="button" preserves the original buttons' a11y role.
  const item = (icon: IconName, label: string, fn: () => void, danger = false) => (
    <DropdownMenuItem
      role="button"
      onSelect={() => fn()}
      className={cn(danger && 'text-destructive focus:text-destructive')}
    >
      <Icon name={icon} /> {label}
    </DropdownMenuItem>
  )

  return (
    <DropdownMenu open={open} onOpenChange={setOpen} modal={false}>
      <Tooltip label="More">
        <DropdownMenuTrigger asChild>
          <button
            aria-label="More"
            onClick={(e) => e.stopPropagation()}
            className={cn(
              'grid h-6 w-[26px] place-items-center rounded-md transition-colors',
              open ? 'bg-accent text-foreground' : 'cursor-pointer bg-transparent text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
          >
            <Icon name="more" />
          </button>
        </DropdownMenuTrigger>
      </Tooltip>
      <DropdownMenuContent
        align="end"
        className="dp-panel w-[184px]"
        // don't yank focus back to the trigger on close — the shelf/trigger may unmount, and the
        // "Rename" flow needs the freshly-mounted title input to keep focus (matches the old popover)
        onCloseAutoFocus={(e) => e.preventDefault()}
        onClick={(e) => e.stopPropagation()}
      >
        {item('rename', 'Rename', () => window.dispatchEvent(new CustomEvent('dp-rename', { detail: { id } })))}
        {item('play', 'Run details', () => openPanel(id, 'run'))}
        {item('duplicate', 'Duplicate', () => duplicate(id))}
        {canBypass && item('power', 'Bypass (pass data through)', () => bypass(id))}
        {item('mute', 'Disable (+ downstream)', () => disable(id))}
        {item('export', 'Export data', () => exportNode(id))}
        {item('lineage', 'Lineage', () => openPanel(id, 'lineage'))}
        <DropdownMenuSeparator />
        {item('trash', 'Delete', () => removeNode(id), true)}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
