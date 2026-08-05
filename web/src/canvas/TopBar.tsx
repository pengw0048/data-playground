import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { roleCanEdit, useStore, type GraphRunState } from '../store/graph'
import { Icon, type IconName } from '../ui/Icon'
import { ProgressBar } from '../ui/controls'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import { SettingsModal } from '../panels/SettingsModal'
import { CanvasSettingsModal } from '../panels/CanvasSettingsModal'
import { ImportPipelineModal } from '../panels/ImportPipelineModal'
import { RunHistoryModal } from '../panels/RunHistoryModal'
import { VersionHistoryModal } from '../panels/VersionHistoryModal'
import { ShareModal } from '../panels/ShareModal'
import { crdtUndoActive } from '../collab/undo'
import { getThemeMode, setThemeMode, type ThemeMode } from '../theme/mode'
import type { ExecutionTargetInfo, KernelInfo } from '../types/api'
import { exportCanvas } from '../lib/exporters'
import { NativeCanvasImportModal } from '../panels/NativeCanvasImportModal'
import { CanvasCopyModal } from '../panels/CanvasCopyModal'
import { CanvasWorkspaceLocation } from './CanvasWorkspaceLocation'
import { CanvasInboxPopover } from './CanvasInboxPopover'
import { ConfirmationDialog } from '../components/ConfirmationDialog'

/** Step counts for the single whole-graph pass; the run reports every node it will execute. */
function rerunAllProgress(graphRun: GraphRunState | null) {
  const perNode = graphRun?.status?.perNode ?? []
  const done = perNode.filter((step) => step.status === 'done' || step.status === 'failed').length
  return { done, total: perNode.length, value: graphRun?.status?.progress ?? 0 }
}

type OpenSettingsDetail = HTMLElement | {
  category?: string
  trigger?: HTMLElement | null
  onClose?: () => void
}

export function TopBar() {
  const kernelUp = useStore((s) => s.kernelUp)
  const kernelInfo = useStore((s) => s.kernelInfo)
  const saved = useStore((s) => s.saved)
  const currentDraftId = useStore((s) => s.currentDraftId)
  const currentDraft = useStore((s) => s.localDrafts.find((draft) => draft.draftId === s.currentDraftId))
  const canvasRole = useStore((s) => s.canvasRole)
  const canEdit = roleCanEdit(canvasRole)
  const rerunAll = useStore((s) => s.rerunAll)
  const cancelGraphRun = useStore((s) => s.cancelGraphRun)
  const graphRun = useStore((s) => s.graphRun)
  const hasRunEvidence = useStore((s) => s.doc.nodes.some((node) => (
    Boolean(node.data.status) && node.data.status !== 'draft'
  )))
  const authEnabled = useStore((s) => s.authEnabled)
  const graphProgress = rerunAllProgress(graphRun)
  // in a co-edit session undo/redo go through the CRDT manager (not the snapshot stacks), so enable the
  // buttons whenever collab is active — pressing with empty history is a harmless no-op
  const canUndo = useStore((s) => s.past.length > 0) || crdtUndoActive()
  const canRedo = useStore((s) => s.future.length > 0) || crdtUndoActive()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsCategory, setSettingsCategory] = useState<string | undefined>()
  const [canvasSettingsOpen, setCanvasSettingsOpen] = useState(false)
  const [runsOpen, setRunsOpen] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [nativeImportOpen, setNativeImportOpen] = useState(false)
  const [copyOpen, setCopyOpen] = useState(false)
  const [workspaceReturnDestination, setWorkspaceReturnDestination] = useState<string | null | undefined>(undefined)
  const settingsTrigger = useRef<HTMLElement | null>(null)
  const settingsAfterClose = useRef<(() => void) | null>(null)
  const saveLabel = !canEdit
    ? (canvasRole === 'viewer' ? 'view only' : 'read only')
    : currentDraft?.syncState === 'conflict'
      ? 'sync conflict'
      : currentDraft?.syncState === 'error'
        ? 'draft not saved'
        : !kernelUp
          ? 'offline · server state unknown'
        : currentDraftId
          ? (currentDraft?.syncState === 'syncing' ? 'syncing…' : 'saved locally')
    : saved
      ? 'saved'
      : 'saving…'

  // let anything (e.g. the agent's "Configure a model" CTA) open Settings
  useEffect(() => {
    const onOpen = (event: Event) => {
      const detail = (event as CustomEvent<OpenSettingsDetail>).detail
      if (detail instanceof HTMLElement) {
        settingsTrigger.current = detail
        settingsAfterClose.current = null
        setSettingsCategory(undefined)
      } else {
        settingsTrigger.current = detail?.trigger ?? document.querySelector('[data-testid="app-menu"]')
        settingsAfterClose.current = detail?.onClose ?? null
        setSettingsCategory(detail?.category)
      }
      setSettingsOpen(true)
    }
    window.addEventListener('dp-open-settings', onOpen)
    return () => window.removeEventListener('dp-open-settings', onOpen)
  }, [])

  const openSettings = (trigger: HTMLElement) => {
    settingsTrigger.current = trigger
    settingsAfterClose.current = null
    setSettingsCategory(undefined)
    setSettingsOpen(true)
  }
  const closeSettings = () => {
    const afterClose = settingsAfterClose.current
    settingsAfterClose.current = null
    setSettingsOpen(false)
    requestAnimationFrame(() => {
      settingsTrigger.current?.focus()
      afterClose?.()
    })
  }
  const navigateToWorkspace = (resourceId: string | null | undefined) => {
    const store = useStore.getState()
    if (resourceId === undefined) {
      // No placement was proven (local draft/unplaced Canvas): retain the generic entry.
      store.setView('workspace')
      return
    }
    // A non-empty search and the Datasets lens do not establish that the Canvas is visible at this
    // placement. Reset them atomically so #705 emits one owned navigation destination.
    store.switchWorkspaceScope('all', { resourceId, searchQuery: '', datasetQuery: '' })
  }

  return (
    <>
      <div data-layout-region="canvas-top-chrome"
        style={{ position: 'absolute', top: kernelUp ? 16 : 48, left: 20, right: 20, zIndex: 15, display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', alignItems: 'center', gap: 12, pointerEvents: 'none' }}>
        <div style={{ pointerEvents: 'auto' }} className="flex w-fit min-w-0 max-w-full items-center gap-2 overflow-hidden">
          <AppMenu
            onWorkspace={() => navigateToWorkspace(workspaceReturnDestination)}
            onSettings={() => openSettings(document.querySelector<HTMLElement>('[data-testid="app-menu"]')!)}
            onImport={() => setImportOpen(true)}
            onNativeImport={() => setNativeImportOpen(true)}
            onCanvasSettings={() => setCanvasSettingsOpen(true)}
            onRunHistory={() => setRunsOpen(true)}
            onVersionHistory={() => setVersionsOpen(true)}
            onNativeExport={() => { void exportCanvas() }}
            onCopy={() => setCopyOpen(true)}
            copyable={!!canvasRole && kernelUp && saved && !currentDraftId}
          />
          <CanvasInboxPopover />
          <span aria-hidden className="mx-0.5 h-5 w-px shrink-0 bg-border" />
          <CanvasTitle />
          <CanvasFavoriteButton />
          {canEdit && currentDraft?.syncState === 'conflict'
            ? <button data-testid="autosave" type="button" aria-label="Sync conflict — choose how to continue"
                title={currentDraft.lastError} onClick={() => useStore.getState().notifyLocalDraftConflict(currentDraft.draftId)}
                className="ml-0.5 shrink-0 rounded px-1 text-[11px] font-semibold text-destructive underline decoration-dotted underline-offset-2 hover:bg-accent">· {saveLabel}</button>
            : <span data-testid="autosave" title={!canEdit ? 'Editing is disabled for your current access level' : currentDraft?.lastError ?? (!kernelUp ? 'Offline — server save state is unknown. Edits remain cached in this browser.' : undefined)} className={cn('ml-0.5 shrink-0 text-[11px]', currentDraft?.syncState === 'conflict' || currentDraft?.syncState === 'error' || !kernelUp ? 'text-destructive' : 'text-muted-foreground')}>· {saveLabel}</span>}
          <span className="ml-1.5 inline-flex shrink-0 gap-0.5">
            <IconBtn name="undo" label="Undo" disabled={!canEdit || !canUndo} onClick={() => useStore.getState().undo()} />
            <IconBtn name="redo" label="Redo" disabled={!canEdit || !canRedo} onClick={() => useStore.getState().redo()} />
          </span>
        </div>
        <div data-testid="canvas-run-controls" style={{ pointerEvents: 'auto' }} className="flex items-center gap-2.5">
          <PeerAvatars />
          <ExecutionTargetMenu kernelUp={kernelUp} kernelInfo={kernelInfo} canEdit={canEdit} />
          <span className="relative">
            <Button onClick={() => graphRun ? void cancelGraphRun() : rerunAll()}
              disabled={!canEdit || !kernelUp || (!!graphRun && !graphRun.runId)}
              title={!canEdit ? 'View-only canvas' : !kernelUp ? 'Offline — reconnect before running'
                : graphRun?.runId ? 'Stop the whole-graph run'
                  : graphRun ? 'Starting the whole-graph run'
                    : hasRunEvidence ? 'Re-run the whole graph' : 'Run the whole graph'}
              size="sm" className="rounded-full bg-foreground text-background hover:bg-foreground/90">
              {graphRun?.runId
                ? <><Icon name="stop" size={12} /> Stop {graphProgress.done}/{graphProgress.total}</>
                : graphRun
                  ? <><span className="dp-running-glyph">●</span> Starting…</>
                : hasRunEvidence
                  ? <><Icon name="refresh" size={13} /> Rerun all</>
                  : <><Icon name="play" size={13} /> Run all</>}
            </Button>
            {graphRun?.status && (
              <span className="absolute -bottom-2 left-1 right-1 block">
                <ProgressBar value={graphProgress.value}
                  label={`Re-running the whole graph — ${graphProgress.done} of ${graphProgress.total} steps done`} />
              </span>
            )}
          </span>
          <Button data-testid="share-btn" onClick={() => setShareOpen(true)} title={authEnabled ? 'Share this canvas' : 'Copy a link to this canvas'} size="sm" className="rounded-full">
            <Icon name="link" size={13} /> {authEnabled ? 'Share' : 'Copy link'}
          </Button>
        </div>
      </div>
      <div data-layout-region="canvas-top-chrome"
        style={{ position: 'absolute', top: kernelUp ? 50 : 82, left: 74, zIndex: 15, maxWidth: 'calc(100% - 94px)' }}>
        <CanvasWorkspaceLocation onReturnDestination={setWorkspaceReturnDestination} onNavigate={navigateToWorkspace} />
      </div>
      {settingsOpen && <SettingsModal onClose={closeSettings} initialCategory={settingsCategory} />}
      {canvasSettingsOpen && <CanvasSettingsModal onClose={() => setCanvasSettingsOpen(false)} />}
      {runsOpen && <RunHistoryModal onClose={() => setRunsOpen(false)} />}
      {versionsOpen && <VersionHistoryModal onClose={() => setVersionsOpen(false)} />}
      {shareOpen && <ShareModal onClose={() => setShareOpen(false)} />}
      {importOpen && <ImportPipelineModal onClose={() => setImportOpen(false)} />}
      {nativeImportOpen && <NativeCanvasImportModal onClose={() => setNativeImportOpen(false)} />}
      {copyOpen && <CanvasCopyModal source={{ canvasId: useStore.getState().doc.id, version: useStore.getState().doc.version, name: useStore.getState().doc.name ?? 'Untitled canvas' }} onClose={() => setCopyOpen(false)} />}
    </>
  )
}

function CanvasFavoriteButton() {
  const canvasId = useStore((state) => state.doc.id)
  const canvasName = useStore((state) => state.doc.name || 'Untitled canvas')
  const canvasRole = useStore((state) => state.canvasRole)
  const pushToast = useStore((state) => state.pushToast)
  const [favorited, setFavorited] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    setFavorited(null)
    setBusy(false)
    if (!canvasRole || !canvasId) return () => { cancelled = true }
    void api.workspaceFavoriteStatus([`canvas:${canvasId}`]).then((status) => {
      if (!cancelled) setFavorited(status.favorited.includes(`canvas:${canvasId}`))
    }).catch(() => {
      if (!cancelled) setFavorited(null)
    })
    return () => { cancelled = true }
  }, [canvasId, canvasRole])

  if (favorited === null) return null
  const label = favorited
    ? `Remove ${canvasName} from Favorites`
    : `Add ${canvasName} to Favorites`
  return <button type="button" aria-label={label} aria-pressed={favorited} title={label}
    disabled={busy} onClick={() => {
      const resourceId = `canvas:${canvasId}`
      const next = !favorited
      setBusy(true)
      void (next ? api.workspaceFavoriteAdd(resourceId) : api.workspaceFavoriteRemove(resourceId))
        .then(() => {
          if (useStore.getState().doc.id === canvasId) setFavorited(next)
        })
        .catch((error: unknown) => {
          if (useStore.getState().doc.id === canvasId) {
            const message = error instanceof Error ? error.message : String(error)
            pushToast(`Could not update favorite: ${message}`, 'error')
          }
        })
        .finally(() => {
          if (useStore.getState().doc.id === canvasId) setBusy(false)
        })
    }}
    className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50">
    <Icon name="star" size={14} filled={favorited} />
  </button>
}

const AUTOMATIC_EXECUTION = '__automatic__'

function ExecutionTargetMenu({ kernelUp, kernelInfo, canEdit }: {
  kernelUp: boolean
  kernelInfo: KernelInfo | null
  canEdit: boolean
}) {
  const selected = useStore((state) => state.doc.executionBackend)
  const setExecutionBackend = useStore((state) => state.setExecutionBackend)
  const targets = kernelInfo?.executionTargets ?? []
  const selectedTarget = targets.find((target) => target.name === selected)
  const unavailable = selected && !selectedTarget
  const label = selectedTarget?.label ?? (unavailable ? 'Target unavailable' : 'Automatic')
  const grouped = (kind: ExecutionTargetInfo['kind']) => targets.filter((target) => target.kind === kind)

  const targetItem = (target: ExecutionTargetInfo) => (
    <DropdownMenuRadioItem key={target.name} value={target.name} className="items-start py-2 pl-8">
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex items-center gap-2 text-[12px] font-semibold text-foreground">
          {target.label}
          {target.substrate && <span className="rounded bg-muted px-1.5 py-0.5 text-[9px] font-medium text-muted-foreground">{target.substrate}</span>}
        </span>
        <span className="whitespace-normal text-[10.5px] leading-snug text-muted-foreground">{target.description}</span>
      </span>
    </DropdownMenuRadioItem>
  )

  return <DropdownMenu modal={false}>
    <DropdownMenuTrigger asChild>
      <button type="button" disabled={!kernelUp || !canEdit}
        aria-label={`Execution target: ${label}`}
        title={!canEdit ? 'View-only Canvas' : !kernelUp ? 'Offline' : 'Choose where full Canvas runs execute'}
        className="inline-flex h-8 max-w-[210px] items-center gap-1.5 rounded-full border border-border bg-card px-2.5 text-[11px] font-semibold text-foreground shadow-sm hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${kernelUp && !unavailable ? 'bg-green-500' : 'bg-amber-500'}`} />
        <Icon name="server" size={13} />
        <span className="truncate">{label}</span>
        <Icon name="chevronDown" size={11} />
      </button>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="end" className="w-[360px] p-1.5">
      <div className="px-2 pb-1.5 pt-1 text-[11px] font-semibold text-foreground">Run this Canvas on</div>
      <DropdownMenuRadioGroup value={selected ?? AUTOMATIC_EXECUTION}
        onValueChange={(value) => setExecutionBackend(value === AUTOMATIC_EXECUTION ? null : value)}>
        <DropdownMenuRadioItem value={AUTOMATIC_EXECUTION} className="items-start py-2 pl-8">
          <span className="flex flex-col gap-0.5">
            <span className="text-[12px] font-semibold text-foreground">Automatic</span>
            <span className="whitespace-normal text-[10.5px] leading-snug text-muted-foreground">Use the workspace default and automatic resource placement.</span>
          </span>
        </DropdownMenuRadioItem>
        {unavailable && <DropdownMenuRadioItem value={selected} disabled className="items-start py-2 pl-8">
          <span className="flex flex-col gap-0.5">
            <span className="text-[12px] font-semibold text-destructive">{selected}</span>
            <span className="text-[10.5px] text-muted-foreground">This saved target is not configured on this deployment.</span>
          </span>
        </DropdownMenuRadioItem>}
        {grouped('interactive').length > 0 && <>
          <DropdownMenuSeparator />
          <div className="px-2 py-1 text-[9.5px] font-semibold uppercase tracking-wide text-muted-foreground">Interactive worker</div>
          {grouped('interactive').map(targetItem)}
        </>}
        {grouped('job').length > 0 && <>
          <DropdownMenuSeparator />
          <div className="px-2 py-1 text-[9.5px] font-semibold uppercase tracking-wide text-muted-foreground">Full-run jobs</div>
          {grouped('job').map(targetItem)}
        </>}
      </DropdownMenuRadioGroup>
      <DropdownMenuSeparator />
      <p className="px-2 py-1.5 text-[10px] leading-snug text-muted-foreground">Previews remain interactive; full runs use the target saved with this Canvas.</p>
    </DropdownMenuContent>
  </DropdownMenu>
}

// Live presence: avatars of other people currently on this canvas (realtime collab).
function PeerAvatars() {
  const peers = useStore((s) => s.peers)
  const list = Object.entries(peers)
  if (list.length === 0) return null
  return (
    <div className="flex items-center" title={`${list.length} other${list.length > 1 ? 's' : ''} here`}>
      {list.slice(0, 5).map(([id, p], i) => (
        <span
          key={id}
          className="grid h-[26px] w-[26px] place-items-center rounded-full border-2 border-background text-[11px] font-bold text-white shadow-sm"
          style={{ background: p.color, marginLeft: i === 0 ? 0 : -8 }}
        >
          {(p.name || '?').slice(0, 1).toUpperCase()}
        </span>
      ))}
    </div>
  )
}

// The primary menu owns both global destinations and the current Canvas lifecycle. Keeping one
// predictable entry point avoids a second, ambiguous overflow trigger beside the Canvas title.
export function AppMenu({
  onWorkspace, onSettings, onImport, onNativeImport, onCanvasSettings, onRunHistory,
  onVersionHistory, onNativeExport, onCopy, copyable,
}: {
  onWorkspace: () => void
  onSettings: () => void
  onImport: () => void
  onNativeImport: () => void
  onCanvasSettings: () => void
  onRunHistory: () => void
  onVersionHistory: () => void
  onNativeExport: () => void
  onCopy: () => void
  copyable: boolean
}) {
  const foreignImporterAvailable = useStore((s) => s.kernelInfo?.capabilities.includes('pipeline-importer') ?? false)
  const doc = useStore((s) => s.doc)
  const currentDraftId = useStore((s) => s.currentDraftId)
  const currentDraftSyncing = useStore((s) => s.localDrafts.some((draft) => (
    draft.draftId === s.currentDraftId && draft.syncState === 'syncing'
  )))
  const canvasRole = useStore((s) => s.canvasRole)
  const deleteFile = useStore((s) => s.deleteFile)
  const discardLocalDraft = useStore((s) => s.discardLocalDraft)
  const [themeMode, setVisibleThemeMode] = useState<ThemeMode>(getThemeMode)
  const [deleteTarget, setDeleteTarget] = useState<{
    canvasId: string; draftId: string | null; name: string
  } | null>(null)
  useEffect(() => {
    const sync = () => setVisibleThemeMode(getThemeMode())
    window.addEventListener('dp-theme-change', sync)
    return () => window.removeEventListener('dp-theme-change', sync)
  }, [])
  return (
    <>
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button data-testid="app-menu" title="Data Playground menu" aria-label="Data Playground menu"
          className="grid h-7 w-7 cursor-pointer place-items-center rounded-md border-0 bg-transparent text-foreground hover:bg-accent">
          <Icon name="menu" size={17} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[210px]">
        <DropdownMenuItem onSelect={onWorkspace}><Icon name="chevronLeft" size={14} /> Back to Workspace</DropdownMenuItem>
        <DropdownMenuItem data-testid="import-native-canvas" onSelect={() => setTimeout(onNativeImport)}><Icon name="import" size={14} /> Import native Canvas…</DropdownMenuItem>
        {/* defer modal opens to the next tick — otherwise the menu-item pointerup that's still
            propagating is caught by the just-mounted dialog's dismiss layer and closes it instantly */}
        {foreignImporterAvailable && <DropdownMenuItem data-testid="import-pipeline" onSelect={() => setTimeout(onImport)}><Icon name="import" size={14} /> Import pipeline…</DropdownMenuItem>}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setTimeout(onCanvasSettings)}><Icon name="settings" size={14} /> Canvas settings…</DropdownMenuItem>
        <DropdownMenuItem data-testid="copy-canvas" disabled={!copyable} onSelect={() => setTimeout(onCopy)}><Icon name="duplicate" size={14} /> Save a copy…</DropdownMenuItem>
        <DropdownMenuItem data-testid="export-native-canvas" onSelect={() => setTimeout(onNativeExport)}><Icon name="export" size={14} /> Export native Canvas…</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setTimeout(onRunHistory)}><Icon name="clock" size={14} /> Run history</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setTimeout(onVersionHistory)}><Icon name="refresh" size={14} /> Version history</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuSub>
          <DropdownMenuSubTrigger><Icon name="sun" size={14} /> Appearance</DropdownMenuSubTrigger>
          <DropdownMenuSubContent>
            <DropdownMenuRadioGroup value={themeMode}>
              <DropdownMenuRadioItem value="system" onSelect={() => setThemeMode('system')}>System</DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="light" onSelect={() => setThemeMode('light')}>Light</DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="dark" onSelect={() => setThemeMode('dark')}>Dark</DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
          </DropdownMenuSubContent>
        </DropdownMenuSub>
        <DropdownMenuItem onSelect={() => setTimeout(onSettings)}><Icon name="settings" size={14} /> Settings</DropdownMenuItem>
        {canvasRole === 'owner' && <DropdownMenuSeparator />}
        {canvasRole === 'owner' && <DropdownMenuItem disabled={currentDraftSyncing}
          title={currentDraftSyncing ? 'Wait for syncing to finish before deleting this draft' : undefined}
          onSelect={() => {
          const target = { canvasId: doc.id, draftId: currentDraftId, name: doc.name || 'untitled' }
          setTimeout(() => setDeleteTarget(target))
        }} className="text-destructive focus:text-destructive"><Icon name="trash" size={14} /> {currentDraftSyncing ? 'Syncing local draft…' : currentDraftId ? 'Delete this local draft' : 'Delete this Canvas'}</DropdownMenuItem>}
      </DropdownMenuContent>
    </DropdownMenu>
    <ConfirmationDialog
      open={deleteTarget !== null}
      title={deleteTarget?.draftId ? `Delete local draft “${deleteTarget.name}”?` : `Delete “${deleteTarget?.name ?? 'this Canvas'}”?`}
      description={deleteTarget?.draftId
        ? 'This permanently deletes the changes saved only in this browser. It does not delete a server Canvas. This cannot be undone.'
        : 'This permanently deletes the Canvas for everyone who can access it. This cannot be undone.'}
      confirmLabel={deleteTarget?.draftId ? 'Delete local draft' : 'Delete Canvas'}
      onCancel={() => setDeleteTarget(null)}
      onConfirm={() => {
        const target = deleteTarget
        setDeleteTarget(null)
        if (!target) return
        if (target.draftId) void discardLocalDraft(target.draftId)
        else void deleteFile(target.canvasId)
      }}
    />
    </>
  )
}

export function CanvasTitle() {
  const doc = useStore((s) => s.doc)
  const renameFile = useStore((s) => s.renameFile)
  const canvasRole = useStore((s) => s.canvasRole)
  const canEdit = roleCanEdit(canvasRole)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(doc.name ?? '')
  const original = useRef(doc.name ?? '')
  const editingCanvasId = useRef(doc.id)
  const input = useRef<HTMLInputElement | null>(null)
  const renderedCanvasId = doc.id
  const ownsActiveCanvas = () => (
    editingCanvasId.current === renderedCanvasId
    && useStore.getState().doc.id === renderedCanvasId
  )

  useEffect(() => {
    if (editingCanvasId.current !== doc.id) {
      // TopBar survives browser Back/Forward navigation between Canvases. Never carry an input or
      // its Escape rollback target across that identity boundary.
      editingCanvasId.current = doc.id
      original.current = doc.name ?? ''
      setDraft(doc.name ?? '')
      setEditing(false)
    } else if (!editing) {
      original.current = doc.name ?? ''
      setDraft(doc.name ?? '')
    }
  }, [doc.id, doc.name, editing])

  useEffect(() => {
    if (!editing) return
    input.current?.focus()
    input.current?.select()
  }, [editing])

  const begin = () => {
    if (!canEdit) return
    editingCanvasId.current = doc.id
    original.current = doc.name ?? ''
    setDraft(doc.name ?? '')
    setEditing(true)
  }
  const cancel = () => {
    // A stale input event can arrive after navigation but before React removes the old element.
    // It must not rename the newly active Canvas.
    if (!ownsActiveCanvas()) return
    renameFile(original.current)
    setDraft(doc.name ?? '')
    setEditing(false)
  }

  if (editing && editingCanvasId.current === renderedCanvasId) {
    return <input
      ref={input}
      data-testid="canvas-title-input"
      aria-label="Canvas name"
      value={draft}
      onChange={(event) => {
        if (!ownsActiveCanvas()) return
        setDraft(event.target.value)
        renameFile(event.target.value)
      }}
      onBlur={() => {
        if (ownsActiveCanvas()) setEditing(false)
      }}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          event.currentTarget.blur()
        } else if (event.key === 'Escape') {
          event.preventDefault()
          cancel()
        }
      }}
      className="h-7 w-[clamp(120px,32vw,420px)] min-w-0 rounded-md border border-primary bg-background px-1.5 text-[13.5px] font-semibold text-foreground outline-none"
    />
  }

  const name = doc.name || 'untitled'
  return <button
    type="button"
    data-testid="canvas-title"
    title={canEdit ? `${name} — click to rename` : `${name} — view only`}
    aria-label={canEdit ? `Rename Canvas ${name}` : `Canvas ${name}, view only`}
    disabled={!canEdit}
    onClick={begin}
    className="min-w-0 max-w-[min(42vw,520px)] truncate rounded-md border border-transparent px-1 py-0.5 text-left text-[13.5px] font-semibold text-foreground hover:border-primary/40 hover:bg-primary/5 disabled:cursor-default disabled:hover:border-transparent disabled:hover:bg-transparent"
  >
    {name}
  </button>
}

function IconBtn({ name, label, onClick, disabled }: { name: IconName; label: string; onClick: () => void; disabled?: boolean }) {
  // enabled reads as clearly interactive (foreground); disabled falls to a faded muted tone so the
  // "nothing to undo/redo" state is unmistakable rather than a subtle opacity shift on the same color
  return (
    <Button variant="ghost" size="icon" aria-label={label} title={label} onClick={onClick} disabled={disabled}
      className={cn('h-7 w-7', disabled ? 'text-muted-foreground' : 'text-foreground')}>
      <Icon name={name} size={14} />
    </Button>
  )
}
