import { useEffect, useRef, useState } from 'react'
import { roleCanEdit, useStore } from '../store/graph'
import { Icon, type IconName } from '../ui/Icon'
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
import { KernelBadge } from './KernelBadge'
import { exportCanvas } from '../lib/exporters'
import { NativeCanvasImportModal } from '../panels/NativeCanvasImportModal'
import { CanvasCopyModal } from '../panels/CanvasCopyModal'
import { CanvasWorkspaceLocation } from './CanvasWorkspaceLocation'
import { CanvasInboxPopover } from './CanvasInboxPopover'

export function TopBar() {
  const kernelUp = useStore((s) => s.kernelUp)
  const kernelInfo = useStore((s) => s.kernelInfo)
  const saved = useStore((s) => s.saved)
  const currentDraftId = useStore((s) => s.currentDraftId)
  const currentDraft = useStore((s) => s.localDrafts.find((draft) => draft.draftId === s.currentDraftId))
  const canvasRole = useStore((s) => s.canvasRole)
  const canEdit = roleCanEdit(canvasRole)
  const rerunAll = useStore((s) => s.rerunAll)
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
      const detail = (event as CustomEvent<HTMLElement | { category?: string; trigger?: HTMLElement | null }>).detail
      if (detail instanceof HTMLElement) {
        settingsTrigger.current = detail
        setSettingsCategory(undefined)
      } else {
        settingsTrigger.current = detail?.trigger ?? document.querySelector('[data-testid="app-menu"]')
        setSettingsCategory(detail?.category)
      }
      setSettingsOpen(true)
    }
    window.addEventListener('dp-open-settings', onOpen)
    return () => window.removeEventListener('dp-open-settings', onOpen)
  }, [])

  const openSettings = (trigger: HTMLElement) => {
    settingsTrigger.current = trigger
    setSettingsCategory(undefined)
    setSettingsOpen(true)
  }
  const closeSettings = () => {
    setSettingsOpen(false)
    requestAnimationFrame(() => settingsTrigger.current?.focus())
  }
  const navigateToWorkspace = (resourceId: string | null | undefined) => {
    const store = useStore.getState()
    if (resourceId === undefined) {
      // No placement was proven (local draft/unplaced Canvas): retain the existing generic entry.
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
        style={{ position: 'absolute', top: kernelUp ? 16 : 48, left: 20, right: 20, zIndex: 15, display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', alignItems: 'center', gap: 12 }}>
        <div className="flex min-w-0 items-center gap-2 overflow-hidden">
          <AppMenu onWorkspace={() => navigateToWorkspace(null)} onSettings={() => openSettings(document.querySelector<HTMLElement>('[data-testid="app-menu"]')!)} onImport={() => setImportOpen(true)} onNativeImport={() => setNativeImportOpen(true)} />
          <CanvasInboxPopover />
          <span aria-hidden className="mx-0.5 h-5 w-px shrink-0 bg-border" />
          <CanvasTitle />
          <CanvasOverflowMenu
            onShowInWorkspace={() => navigateToWorkspace(workspaceReturnDestination)}
            onCanvasSettings={() => setCanvasSettingsOpen(true)}
            onRunHistory={() => setRunsOpen(true)}
            onVersionHistory={() => setVersionsOpen(true)}
            onNativeExport={() => { void exportCanvas() }}
            onCopy={() => setCopyOpen(true)}
            copyable={!!canvasRole && kernelUp && saved && !currentDraftId}
          />
          <span data-testid="autosave" title={!canEdit ? 'Editing is disabled for your current access level' : currentDraft?.lastError ?? (!kernelUp ? 'Hub offline — server save state is unknown. Local edits remain cached in this browser.' : undefined)} className={cn('ml-0.5 shrink-0 text-[11px]', currentDraft?.syncState === 'conflict' || currentDraft?.syncState === 'error' || !kernelUp ? 'text-destructive' : 'text-muted-foreground')}>· {saveLabel}</span>
          <span className="ml-1.5 inline-flex shrink-0 gap-0.5">
            <IconBtn name="undo" label="Undo" disabled={!canEdit || !canUndo} onClick={() => useStore.getState().undo()} />
            <IconBtn name="redo" label="Redo" disabled={!canEdit || !canRedo} onClick={() => useStore.getState().redo()} />
          </span>
        </div>
        <div data-testid="canvas-run-controls" className="flex items-center gap-2.5">
          <PeerAvatars />
          <KernelBadge kernelUp={kernelUp} kernelInfo={kernelInfo} />
          <Button onClick={rerunAll} disabled={!canEdit || !kernelUp} title={!canEdit ? 'View-only canvas' : !kernelUp ? 'Hub offline — reconnect before running' : 'Re-run the whole graph'} size="sm" className="rounded-full bg-foreground text-background hover:bg-foreground/90">
            <Icon name="refresh" size={13} /> Rerun all
          </Button>
          <Button data-testid="share-btn" onClick={() => setShareOpen(true)} title="Share this canvas" size="sm" className="rounded-full">
            <Icon name="link" size={13} /> Share
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

// Global destinations and preferences live here. Current-Canvas actions belong to CanvasOverflowMenu.
export function AppMenu({ onWorkspace, onSettings, onImport, onNativeImport }: {
  onWorkspace: () => void
  onSettings: () => void
  onImport: () => void
  onNativeImport: () => void
}) {
  const setJobsQuery = useStore((s) => s.setJobsQuery)
  const setInboxQuery = useStore((s) => s.setInboxQuery)
  const inboxQuery = useStore((s) => s.inboxQuery)
  const newFile = useStore((s) => s.newFile)
  const foreignImporterAvailable = useStore((s) => s.kernelInfo?.capabilities.includes('pipeline-importer') ?? false)
  const [themeMode, setVisibleThemeMode] = useState<ThemeMode>(getThemeMode)
  useEffect(() => {
    const sync = () => setVisibleThemeMode(getThemeMode())
    window.addEventListener('dp-theme-change', sync)
    return () => window.removeEventListener('dp-theme-change', sync)
  }, [])
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button data-testid="app-menu" title="Data Playground menu" aria-label="Data Playground menu"
          className="grid h-7 w-7 cursor-pointer place-items-center rounded-md border-0 bg-transparent text-foreground hover:bg-accent">
          <Icon name="menu" size={17} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[210px]">
        <DropdownMenuItem onSelect={onWorkspace}><Icon name="chevronLeft" size={14} /> Back to Workspace</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => newFile()}><Icon name="plus" size={14} /> New Canvas</DropdownMenuItem>
        <DropdownMenuItem data-testid="import-native-canvas" onSelect={() => setTimeout(onNativeImport)}><Icon name="import" size={14} /> Import native Canvas…</DropdownMenuItem>
        {/* defer modal opens to the next tick — otherwise the menu-item pointerup that's still
            propagating is caught by the just-mounted dialog's dismiss layer and closes it instantly */}
        {foreignImporterAvailable && <DropdownMenuItem data-testid="import-pipeline" onSelect={() => setTimeout(onImport)}><Icon name="import" size={14} /> Import pipeline…</DropdownMenuItem>}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setJobsQuery('')}><Icon name="clock" size={14} /> <MenuDestination label="Jobs" detail="runs and background tasks" /></DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setInboxQuery(inboxQuery)}><Icon name="note" size={14} /> <MenuDestination label="Inbox" detail="my background task results" /></DropdownMenuItem>
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
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function MenuDestination({ label, detail }: { label: string; detail: string }) {
  return <span className="flex min-w-0 flex-1 flex-col items-start leading-tight">
    <span>{label}</span>
    <span aria-hidden className="mt-0.5 whitespace-normal text-[10px] leading-tight text-muted-foreground">{detail}</span>
  </span>
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
    if (editingCanvasId.current === doc.id) renameFile(original.current)
    setDraft(doc.name ?? '')
    setEditing(false)
  }

  if (editing) {
    return <input
      ref={input}
      data-testid="canvas-title-input"
      aria-label="Canvas name"
      value={draft}
      onChange={(event) => {
        setDraft(event.target.value)
        renameFile(event.target.value)
      }}
      onBlur={() => setEditing(false)}
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
    className="min-w-0 max-w-[min(42vw,520px)] truncate rounded-md px-1 py-0.5 text-left text-[13.5px] font-semibold text-foreground hover:bg-accent disabled:cursor-default disabled:hover:bg-transparent"
  >
    {name}
  </button>
}

export function CanvasOverflowMenu({ onShowInWorkspace, onCanvasSettings, onRunHistory, onVersionHistory, onNativeExport, onCopy, copyable }: {
  onShowInWorkspace: () => void
  onCanvasSettings: () => void
  onRunHistory: () => void
  onVersionHistory: () => void
  onNativeExport: () => void
  onCopy: () => void
  copyable: boolean
}) {
  const doc = useStore((s) => s.doc)
  const currentDraftId = useStore((s) => s.currentDraftId)
  const canvasRole = useStore((s) => s.canvasRole)
  const deleteFile = useStore((s) => s.deleteFile)
  const discardLocalDraft = useStore((s) => s.discardLocalDraft)

  return <DropdownMenu modal={false}>
    <DropdownMenuTrigger asChild>
      <Button data-testid="canvas-menu" variant="ghost" size="icon" aria-label="Canvas actions" title="Canvas actions" className="h-7 w-7 shrink-0">
        <Icon name="more" size={15} />
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="start" className="w-[210px]">
      <DropdownMenuItem onSelect={onShowInWorkspace}><Icon name="grid" size={14} /> Show in Workspace</DropdownMenuItem>
      <DropdownMenuItem onSelect={() => setTimeout(onCanvasSettings)}><Icon name="settings" size={14} /> Canvas settings…</DropdownMenuItem>
      <DropdownMenuSeparator />
      <DropdownMenuItem data-testid="copy-canvas" disabled={!copyable} onSelect={() => setTimeout(onCopy)}><Icon name="duplicate" size={14} /> Save a copy…</DropdownMenuItem>
      <DropdownMenuItem data-testid="export-native-canvas" onSelect={() => setTimeout(onNativeExport)}><Icon name="export" size={14} /> Export native Canvas…</DropdownMenuItem>
      <DropdownMenuSeparator />
      <DropdownMenuItem onSelect={() => setTimeout(onRunHistory)}><Icon name="clock" size={14} /> Run history</DropdownMenuItem>
      <DropdownMenuItem onSelect={() => setTimeout(onVersionHistory)}><Icon name="refresh" size={14} /> Version history</DropdownMenuItem>
      {canvasRole === 'owner' && <DropdownMenuSeparator />}
      {canvasRole === 'owner' && <DropdownMenuItem onSelect={() => currentDraftId ? void discardLocalDraft(currentDraftId) : void deleteFile(doc.id)} className="text-destructive focus:text-destructive"><Icon name="trash" size={14} /> {currentDraftId ? 'Delete this local draft' : 'Delete this Canvas'}</DropdownMenuItem>}
    </DropdownMenuContent>
  </DropdownMenu>
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
