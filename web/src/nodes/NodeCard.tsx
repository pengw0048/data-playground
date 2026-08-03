import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useUpdateNodeInternals } from '@xyflow/react'
import { kindAccent, status as statusTok, statusText } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Tooltip } from '../ui/Tooltip'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'
import {
  ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuSeparator,
  ContextMenuShortcut, ContextMenuTrigger,
} from '@/components/ui/context-menu'
import { cn } from '@/lib/utils'
import { Port } from './Port'
import { useNodeTransientSurface } from './nodeTransientSurface'
import { getSpec, nodeOutputs, type NodeSpec } from './registry'
import { nodeInvalidReason } from './generic'
import { useInputColumns, useSchemaWarnings } from './fields'
import {
  useStore, nodeRunnable, isDisabled, roleCanEdit, hasConfiguredMergeColumnsWrite, hasConfiguredManagedSidecarMerge, hasConfiguredUpsertWrite, type PanelKind,
} from '../store/graph'
import { exportNode } from '../lib/exporters'
import type { NodeData } from '../types/graph'

const KINDS_WITH_CODE = new Set(['transform', 'sql'])
const fmtMs = (ms: number) => (ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`)

export function NodeCard({ id, data, children, metaOverride }: {
  id: string
  data: NodeData
  children?: ReactNode
  metaOverride?: ReactNode
}) {
  const node = useStore((s) => s.doc.nodes.find((n) => n.id === id))
  const spec = getSpec(node?.type ?? 'transform') as NodeSpec | undefined
  const selected = useStore((s) => s.selectedIds.includes(id))
  const canEdit = useStore((s) => roleCanEdit(s.canvasRole))
  const kernelUp = useStore((s) => s.kernelUp)
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
  const runState = useStore((s) => s.runs[id]?.phase)
  const runnable = useStore((s) => nodeRunnable(s.doc, id))
  const configuredMerge = useStore((s) => hasConfiguredMergeColumnsWrite(s.doc, id))
  const configuredManagedSidecarMerge = useStore((s) => hasConfiguredManagedSidecarMerge(s.doc, id))
  const configuredUpsert = useStore((s) => hasConfiguredUpsertWrite(s.doc, id))
  // hover drives the action shelf. The shelf is a DOM descendant of this wrapper (just positioned
  // below it), so the wrapper's own enter/leave already covers card↔shelf travel — moving between
  // them never leaves the subtree, so onMouseLeave doesn't fire. A short grace delay on leave then
  // debounces the final exit so a quick brush-past doesn't flicker the shelf.
  const [hover, setHover] = useState(false)
  const [renameHover, setRenameHover] = useState(false)
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
  const libraryTransform = kind === 'transform' && data.config.source === 'library'
  const configuredLibraryTransform = libraryTransform
    && typeof data.config.processor === 'string' && data.config.processor.length > 0
    && typeof data.config.version === 'string' && data.config.version.length > 0
  const accent = kindAccent[kind] ?? '#8a8f98'
  const st = statusTok[data.status] ?? statusTok.draft
  const bypassed = !!data.bypassed
  const disabled = !!data.disabled
  const off = disabled || offDownstream  // dimmed either way; only self-disabled shows the badge
  const hasCode = KINDS_WITH_CODE.has(kind) && (!libraryTransform || configuredLibraryTransform)
  const busy = runState === 'running' || runState === 'estimating'
  const inputColumns = useInputColumns(id)
  const numericDrafts = useStore((s) => s.numericParamDrafts[id])
  const invalid = node ? nodeInvalidReason(node, inputColumns, numericDrafts) : null   // e.g. "order by is required"
  const warnings = useSchemaWarnings(id)   // soft cue: config references a column not in the input
  const sizeEst = useStore((s) => s.sizes[id])   // conservative pre-run size estimate (card hint)
  // the action shelf is revealed on hover / sole-selection / while running, so a resting card is clean
  // and a multi-card marquee doesn't strand a shelf under every selected node. An off (disabled or
  // downstream-of-disabled) node only shows it when selected — brushing past shouldn't pop a dead toolbar.
  const showShelf = soleSelected || busy || (hover && !off)

  const tag = (spec?.tag ?? kind).toUpperCase()

  return (
    <ContextMenu>
    <ContextMenuTrigger asChild>
    <div className={cn('dp-no-select relative w-[232px]', off && 'opacity-45')}
      onContextMenu={(event) => {
        event.stopPropagation()
        if (!useStore.getState().selectedIds.includes(id)) useStore.getState().select(id)
      }}
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
        data-dp-card data-selected={selected || undefined}
        className={cn(
          'overflow-hidden rounded-lg border bg-card shadow-sm transition-[box-shadow,border-color] duration-100',
          !bypassed && (selected ? 'border-primary' : 'border-border'),
          selected && 'ring-2 ring-primary/20',
          canEdit && hover && !selected && !bypassed && 'border-primary/60 ring-1 ring-primary/15',
          renameHover && 'border-primary ring-2 ring-primary/20',
        )}
        style={{
          ...(bypassed ? { border: `1.5px dashed ${accent}` } : {}),
          ...(off ? { filter: 'grayscale(0.7)' } : {}),
        }}
      >
        <div className="flex">
          <div className="min-w-0 flex-1 px-3 pb-3 pt-[11px]">
            {/* header */}
            <div className="flex items-center gap-[7px]">
              <span
                className={cn('w-3 text-center text-xs leading-none', data.status === 'running' && 'dp-running-glyph')}
                style={{ color: statusText[data.status] ?? statusText.draft }}
                title={st.label}
              >
                {st.glyph}
              </span>
              <EditableTitle id={id} title={data.title} selected={selected} canEdit={canEdit}
                onRenameHover={setRenameHover} />
              <span className="flex-1" />
              {disabled && (
                <span className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-[8.5px] font-bold tracking-[0.5px] text-amber-700 dark:bg-amber-500/15 dark:text-amber-300">
                  DISABLED
                </span>
              )}
              {bypassed && !disabled && (
                <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[8.5px] font-bold tracking-[0.5px] text-muted-foreground" title="Bypassed — input flows straight through, this step is skipped">
                  BYPASSED
                </span>
              )}
              {(data.config as Record<string, unknown>)?.checkpoint ? (
                <span className="shrink-0 text-[9px] leading-none text-primary" title="This step’s result is saved for reuse">●</span>
              ) : null}
              <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[8.5px] font-semibold tracking-[0.6px] text-muted-foreground">
                {tag}
              </span>
            </div>

            {/* meta */}
            <div data-testid="node-meta" className="mt-[5px] min-h-4 truncate text-[11.5px] text-muted-foreground">
              {metaOverride ?? data.meta ?? ''}
            </div>

            {/* soft schema cue: config points at a column not in the input (never blocks a run) */}
            {kind === 'filter' && invalid && !off && (
              <div className="mt-0.5 truncate text-[10.5px] text-amber-700 dark:text-amber-300" title={invalid}>
                ⚠ {invalid}
              </div>
            )}
            {!invalid && warnings.length > 0 && !off && (
              <div className="mt-0.5 truncate text-[10.5px] text-amber-700 dark:text-amber-300" title={warnings.join(' · ')}>
                ⚠ {warnings[0]}
              </div>
            )}

            {/* last run stats — so a completed node carries its result (rows · time) at a glance */}
            {data.status === 'latest' && data.lastRun && (
              <div className="mt-0.5 truncate text-[10.5px] tabular-nums text-muted-foreground/85">
                {data.lastRun.outputCount != null
                  ? `${data.lastRun.outputCount.toLocaleString()} output${data.lastRun.outputCount === 1 ? '' : 's'}`
                  : data.lastRun.rows != null
                    ? `${data.lastRun.rows.toLocaleString()} ${data.lastRun.rows === 1 ? 'row' : 'rows'}`
                    : 'Result'} · {fmtMs(data.lastRun.ms)}
                {data.lastRun.placement === 'distributed' && ' · distributed'}
              </div>
            )}

            {/* size hint — a pre-run estimate. A source's exact count already lives in the meta line, and
                a bounded (filter/dedup) shows a "≤" upper bound rather than a misleading "~". Unknown → nothing. */}
            {!(data.status === 'latest' && data.lastRun) && kind !== 'source' && sizeEst && sizeEst.rows != null && sizeEst.confidence !== 'unknown' && (
              <div className="mt-0.5 truncate text-[10.5px] tabular-nums text-muted-foreground/70"
                title={sizeEst.confidence === 'bounded' ? 'Estimated upper bound — a filter or dedup may output fewer' : 'Estimated output rows (before running)'}>
                {sizeEst.confidence === 'bounded' ? '≤ ' : ''}{sizeEst.rows.toLocaleString()} {sizeEst.rows === 1 ? 'row' : 'rows'}
              </div>
            )}

            {/* a run awaiting confirmation stays visible ON the card (so a rerun-all of several
                sinks doesn't hide all-but-one behind the single floating panel) */}
            {runState === 'confirm' && (
              <button className="nodrag mt-1.5 inline-flex cursor-pointer items-center gap-[5px] rounded-md border border-amber-300 bg-amber-100 px-[9px] py-1 text-[11px] font-semibold text-amber-700 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300"
                onClick={(e) => { e.stopPropagation(); useStore.getState().openPanel(id, 'run') }}>
                <Icon name="power" size={11} /> Confirm run…
              </button>
            )}
            {runState === 'failed' && (
              <button className="nodrag mt-1.5 inline-flex cursor-pointer items-center gap-[5px] rounded-md border border-destructive/30 bg-destructive/10 px-[9px] py-1 text-[11px] font-semibold text-destructive"
                onClick={(e) => { e.stopPropagation(); useStore.getState().openPanel(id, 'run') }}>
                Fix error
              </button>
            )}

            {/* compact body (kind-specific, kept small — P5) */}
            {children && (
              <fieldset disabled={!canEdit} className="contents">
                <div className="mt-2">{children}</div>
              </fieldset>
            )}

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
            name="eye" label={openPanel === 'data'
              ? 'Hide data'
              : !kernelUp
                ? `Hub offline — ${kind === 'chart' ? 'chart result' : 'preview'} unavailable`
                : invalid ?? (runnable
                    ? kind === 'chart' ? 'View chart result' : 'View data'
                    : `Connect a source to ${kind === 'chart' ? 'run this chart' : 'preview'}`)}
            active={openPanel === 'data'} disabled={openPanel !== 'data' && (!kernelUp || !runnable || !!invalid)}
            onClick={() => (openPanel === 'data'
              ? closePanel(id)
              : kind === 'chart' ? togglePanel(id, 'data') : runPreview(id))}
          />
          {/* a source has no compute — its ▶ (a full COUNT/scan) is deliberately not a quick action
              here. Preview shares the same action shelf as every other node; run/materialize stays
              available in the Inspector. */}
          {kind !== 'source' && (
            <ActionIcon
              name={busy ? 'stop' : 'play'}
              label={!kernelUp ? 'Hub offline — run unavailable' : busy ? 'Stop' : invalid ?? (!runnable ? 'Connect a source to run' : configuredManagedSidecarMerge ? 'Review sidecar merge' : configuredMerge ? 'Review column merge' : configuredUpsert ? 'Review keyed upsert' : 'Run up to here')}
              active={openPanel === 'run'}
              disabled={!canEdit || !kernelUp || ((!runnable || !!invalid) && !busy)}
              onClick={() => (busy ? cancelRun(id) : requestRun(id))}
            />
          )}
          {kind !== 'source' && (
            <ActionIcon name="clock" label="Output versions" active={openPanel === 'history'} onClick={() => togglePanel(id, 'history')} />
          )}
          {hasCode && <ActionIcon
            name={libraryTransform ? 'fx' : 'code'}
            label={libraryTransform ? 'View processor definition' : canEdit ? 'Edit code' : 'View code'}
            onClick={() => openCodeFullscreen(id, kind === 'sql' ? 'sql' : 'code', kind === 'sql' ? 'sql' : 'python')}
          />}
          <MoreMenu id={id} kind={kind} canEdit={canEdit} disabled={disabled} bypassed={bypassed} />
        </div>
      )}
    </div>
    </ContextMenuTrigger>
    <NodeContextActions id={id} kind={kind} canEdit={canEdit} disabled={disabled}
      bypassed={bypassed} kernelUp={kernelUp} runnable={runnable} invalid={invalid} />
    </ContextMenu>
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

function EditableTitle({ id, title, selected, canEdit, onRenameHover }: {
  id: string
  title: string
  selected?: boolean
  canEdit: boolean
  onRenameHover: (hovered: boolean) => void
}) {
  const renameDraft = useStore((s) => s.renameDraft?.id === id ? s.renameDraft : null)
  const startRename = useStore((s) => s.startRename)
  const updateRenameDraft = useStore((s) => s.updateRenameDraft)
  const commitRename = useStore((s) => s.commitRename)
  const cancelRename = useStore((s) => s.cancelRename)
  const editing = renameDraft !== null
  const val = renameDraft?.value ?? title
  if (editing) {
    return (
      <input
        autoFocus
        value={val}
        aria-label="Node title"
        onChange={(e) => updateRenameDraft(id, e.target.value)}
        onClick={(e) => e.stopPropagation()}
        onBlur={() => commitRename(id)}
        onKeyDown={(e) => { if (e.key === 'Enter') commitRename(id); if (e.key === 'Escape') cancelRename(id) }}
        className="w-[130px] rounded-sm border border-primary px-1 py-px text-[13.5px] font-semibold text-foreground outline-none"
      />
    )
  }
  return (
    <span
      // click the name of an already-selected node to rename (Figma-style); double-click always works
      onMouseEnter={() => { if (canEdit) onRenameHover(true) }}
      onMouseLeave={() => onRenameHover(false)}
      onClick={(e) => { if (canEdit && selected) { e.stopPropagation(); onRenameHover(false); startRename(id, title) } }}
      onDoubleClick={(e) => { if (canEdit) { e.stopPropagation(); onRenameHover(false); startRename(id, title) } }}
      title={canEdit ? 'Click (when selected) or double-click to rename' : 'View-only'}
      className={cn(
        '-mx-0.5 min-w-0 truncate rounded-sm border border-transparent px-0.5 text-[13.5px] font-semibold text-foreground',
        canEdit && 'cursor-text hover:border-primary/40 hover:bg-primary/5',
      )}
    >
      {title}
    </span>
  )
}

function MoreMenu({ id, kind, canEdit, disabled, bypassed }: { id: string; kind: string; canEdit: boolean; disabled: boolean; bypassed: boolean }) {
  const [open, setOpen] = useState(false)
  const renameRequested = useRef(false)
  useNodeTransientSurface(`node-more-menu:${id}`, open, () => setOpen(false))
  const { bypass, disable, duplicate, removeNode, openPanel } = useStore.getState()
  const startRename = useStore((s) => s.startRename)
  const canBypass = getSpec(kind)?.canBypass

  const requestRename = () => {
    // The title must not mount until the menu's close has committed. Dispatching from onSelect (or
    // even its animation frame) races Radix's focus cleanup: that cleanup can blur and unmount the
    // freshly focused input, silently committing the old title.
    renameRequested.current = true
    setOpen(false)
  }

  // Most items call store actions directly (no Dialogs), so onSelect can run inline and let the menu
  // close normally. Rename uses the deferred focus handoff above. role="button" preserves a11y.
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
        side="top"
        className="dp-panel w-[184px]"
        // don't yank focus back to the trigger on close — the shelf/trigger may unmount, and the
        // "Rename" flow needs the freshly-mounted title input to keep focus (matches the old popover)
        onCloseAutoFocus={(e) => {
          e.preventDefault()
          if (renameRequested.current) {
            renameRequested.current = false
            startRename(id, useStore.getState().doc.nodes.find((node) => node.id === id)?.data.title ?? '')
          }
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {canEdit && item('rename', 'Rename', requestRename)}
        {item('play', 'Run details', () => openPanel(id, 'run'))}
        {canEdit && item('duplicate', 'Duplicate', () => duplicate(id))}
        {canEdit && canBypass && item('power', bypassed ? 'Un-bypass' : 'Bypass (pass data through)', () => bypass(id))}
        {canEdit && item('mute', disabled ? 'Enable' : 'Disable (+ downstream)', () => disable(id))}
        {kind !== 'chart' && item('export', 'Export preview sample (JSON + CSV)', () => exportNode(id))}
        {item('lineage', 'Lineage', () => openPanel(id, 'lineage'))}
        {canEdit && <DropdownMenuSeparator />}
        {canEdit && item('trash', 'Delete', () => removeNode(id), true)}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function NodeContextActions({ id, kind, canEdit, disabled, bypassed, kernelUp, runnable, invalid }: {
  id: string; kind: string; canEdit: boolean; disabled: boolean; bypassed: boolean
  kernelUp: boolean; runnable: boolean; invalid: string | null
}) {
  const renameRequested = useRef(false)
  const canBypass = getSpec(kind)?.canBypass
  const requestRename = () => { renameRequested.current = true }
  const item = (
    icon: IconName, label: string, action: () => void,
    options: { disabled?: boolean; danger?: boolean; shortcut?: string } = {},
  ) => <ContextMenuItem disabled={options.disabled} onSelect={action}
    className={cn(options.danger && 'text-destructive focus:text-destructive')}>
    <Icon name={icon} /> {label}
    {options.shortcut ? <ContextMenuShortcut>{options.shortcut}</ContextMenuShortcut> : null}
  </ContextMenuItem>

  return <ContextMenuContent aria-label="Node actions" className="dp-panel w-[220px]"
    onCloseAutoFocus={(event) => {
      event.preventDefault()
      if (!renameRequested.current) return
      renameRequested.current = false
      const node = useStore.getState().doc.nodes.find((candidate) => candidate.id === id)
      if (node) useStore.getState().startRename(id, node.data.title)
    }}>
    {canEdit && item('rename', 'Rename', requestRename)}
    {item('eye', kind === 'chart' ? 'View chart result' : 'Preview data', () => {
      if (kind === 'chart') useStore.getState().openPanel(id, 'data')
      else void useStore.getState().runPreview(id)
    }, {
      disabled: !kernelUp || !runnable || !!invalid,
    })}
    {item('play', 'Run details', () => useStore.getState().openPanel(id, 'run'))}
    {item('lineage', 'Lineage', () => useStore.getState().openPanel(id, 'lineage'))}
    <ContextMenuSeparator />
    {item('duplicate', 'Copy', () => useStore.getState().copySelection(), { shortcut: '⌘C' })}
    {canEdit && item('duplicate', 'Cut', () => useStore.getState().cutSelection(), { shortcut: '⌘X' })}
    {canEdit && item('duplicate', 'Duplicate', () => useStore.getState().duplicateSelected(), { shortcut: '⌘D' })}
    {canEdit && canBypass && item('power', bypassed ? 'Un-bypass' : 'Bypass', () => useStore.getState().bypass(id))}
    {canEdit && item('mute', disabled ? 'Enable' : 'Disable', () => useStore.getState().disable(id))}
    {canEdit && <ContextMenuSeparator />}
    {canEdit && item('trash', 'Delete', () => useStore.getState().removeSelected(), { danger: true, shortcut: '⌫' })}
  </ContextMenuContent>
}
