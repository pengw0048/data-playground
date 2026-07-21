import { useEffect, useRef, useState } from 'react'
import { roleCanEdit, useStore } from '../store/graph'
import { examples } from '../examples'
import { Icon, type IconName } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import { SettingsModal } from '../panels/SettingsModal'
import { CanvasSettingsModal } from '../panels/CanvasSettingsModal'
import { ImportPipelineModal } from '../panels/ImportPipelineModal'
import { RunHistoryModal } from '../panels/RunHistoryModal'
import { VersionHistoryModal } from '../panels/VersionHistoryModal'
import { ShareModal } from '../panels/ShareModal'
import { crdtUndoActive } from '../collab/undo'
import { resolvedTheme, toggleTheme } from '../theme/mode'
import { KernelBadge } from './KernelBadge'
import { CanvasDraftMenu } from './LocalDrafts'
import { exportCanvas } from '../lib/exporters'
import { NativeCanvasImportModal } from '../panels/NativeCanvasImportModal'
import { CanvasCopyModal } from '../panels/CanvasCopyModal'
import { api } from '../api/client'
import { useExampleCreationIntent } from './useExampleCreationIntent'
import { CanvasWorkspaceLocation } from './CanvasWorkspaceLocation'

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
  const [canvasSettingsOpen, setCanvasSettingsOpen] = useState(false)
  const [runsOpen, setRunsOpen] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [nativeImportOpen, setNativeImportOpen] = useState(false)
  const [copyOpen, setCopyOpen] = useState(false)
  const [inboxUnreadCount, setInboxUnreadCount] = useState<number | null>(null)
  const [workspaceReturnDestination, setWorkspaceReturnDestination] = useState<string | null | undefined>(undefined)
  const settingsTrigger = useRef<HTMLElement | null>(null)
  const saveLabel = !canEdit
    ? (canvasRole === 'viewer' ? 'view only' : 'read only')
    : currentDraft?.syncState === 'conflict'
      ? 'sync conflict'
      : currentDraft?.syncState === 'error'
        ? 'draft not saved'
        : currentDraftId
          ? (currentDraft?.syncState === 'syncing' ? 'syncing…' : 'saved locally')
    : saved
      ? (kernelUp ? 'saved' : 'saved locally')
      : 'saving…'

  // let anything (e.g. the agent's "Configure a model" CTA) open Settings
  useEffect(() => {
    const onOpen = (event: Event) => {
      settingsTrigger.current = (event as CustomEvent<HTMLElement>).detail ?? document.querySelector('[data-testid="app-menu"]')
      setSettingsOpen(true)
    }
    window.addEventListener('dp-open-settings', onOpen)
    return () => window.removeEventListener('dp-open-settings', onOpen)
  }, [])

  // Canvas only shows a count the existing endpoint has actually confirmed. Unlike the Workspace
  // rail's retained shell state, a Canvas fetch failure has no prior Canvas value to preserve.
  useEffect(() => {
    let live = true
    void api.inboxUnreadCount()
      .then(({ count }) => { if (live) setInboxUnreadCount(count) })
      .catch(() => { if (live) setInboxUnreadCount(null) })
    return () => { live = false }
  }, [])

  const openSettings = (trigger: HTMLElement) => {
    settingsTrigger.current = trigger
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
    store.switchWorkspaceScope('all', { resourceId, searchQuery: '' })
  }

  return (
    <>
      <div style={{ position: 'absolute', top: kernelUp ? 16 : 48, left: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 8 }}>
        <AppMenu onWorkspace={() => navigateToWorkspace(workspaceReturnDestination)} onSettings={() => openSettings(document.querySelector<HTMLElement>('[data-testid="app-menu"]')!)} onRunHistory={() => setRunsOpen(true)} onVersionHistory={() => setVersionsOpen(true)} onImport={() => setImportOpen(true)} onNativeImport={() => setNativeImportOpen(true)} onNativeExport={() => { void exportCanvas() }} onCopy={() => setCopyOpen(true)} copyable={!!canvasRole && saved && !currentDraftId} />
        {inboxUnreadCount != null && inboxUnreadCount > 0 && <CanvasInboxIndicator count={inboxUnreadCount} />}
        <span className="text-[13.5px] text-muted-foreground">/</span>
        <FileMenu onCanvasSettings={() => setCanvasSettingsOpen(true)} />
        <span data-testid="autosave" title={!canEdit ? 'Editing is disabled for your current access level' : currentDraft?.lastError ?? (!kernelUp && saved ? 'Kernel offline — saved to this browser only' : undefined)} className={cn('ml-0.5 text-[11px]', currentDraft?.syncState === 'conflict' || currentDraft?.syncState === 'error' ? 'text-destructive' : 'text-muted-foreground')}>· {saveLabel}</span>
        <span className="ml-1.5 inline-flex gap-0.5">
          <IconBtn name="undo" label="Undo" disabled={!canEdit || !canUndo} onClick={() => useStore.getState().undo()} />
          <IconBtn name="redo" label="Redo" disabled={!canEdit || !canRedo} onClick={() => useStore.getState().redo()} />
          <ThemeToggle />
        </span>
      </div>
      <div style={{ position: 'absolute', top: kernelUp ? 45 : 77, left: 74, zIndex: 15, maxWidth: 'calc(100% - 94px)' }}>
        <CanvasWorkspaceLocation onReturnDestination={setWorkspaceReturnDestination} onNavigate={navigateToWorkspace} />
      </div>

      <div style={{ position: 'absolute', top: kernelUp ? 16 : 48, right: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 10 }}>
        <PeerAvatars />
        <KernelBadge kernelUp={kernelUp} kernelInfo={kernelInfo} />
        <Button onClick={rerunAll} disabled={!canEdit} title={canEdit ? 'Re-run the whole graph' : 'View-only canvas'} size="sm" className="rounded-full bg-foreground text-background hover:bg-foreground/90">
          <Icon name="refresh" size={13} /> Rerun all
        </Button>
        <Button data-testid="share-btn" onClick={() => setShareOpen(true)} title="Share this canvas" size="sm" className="rounded-full">
          <Icon name="link" size={13} /> Share
        </Button>
        {/* Settings lives in the app menu (top-left); identity + log out live on the files shell —
            no redundant Settings button / account avatar here. */}
      </div>
      {settingsOpen && <SettingsModal onClose={closeSettings} />}
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

// The app menu (Figma-style hamburger): Back to files, New file, Import pipeline, Run/Version history, Settings.
function AppMenu({ onWorkspace, onSettings, onRunHistory, onVersionHistory, onImport, onNativeImport, onNativeExport, onCopy, copyable }: { onWorkspace: () => void; onSettings: () => void; onRunHistory: () => void; onVersionHistory: () => void; onImport: () => void; onNativeImport: () => void; onNativeExport: () => void; onCopy: () => void; copyable: boolean }) {
  const setJobsQuery = useStore((s) => s.setJobsQuery)
  const setInboxQuery = useStore((s) => s.setInboxQuery)
  const inboxQuery = useStore((s) => s.inboxQuery)
  const newFile = useStore((s) => s.newFile)
  const foreignImporterAvailable = useStore((s) => s.kernelInfo?.capabilities.includes('pipeline-importer') ?? false)
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button data-testid="app-menu" title="Menu" aria-label="App menu"
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border-0 bg-transparent px-1 py-0.5 text-[13.5px] font-bold text-foreground">
          <span className="grid h-5 w-5 place-items-center rounded-[5px] bg-foreground text-xs font-bold text-background" aria-hidden>D</span>
          <span className="text-muted-foreground" aria-hidden><Icon name="chevronDown" size={12} /></span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[210px]">
        <DropdownMenuItem onSelect={onWorkspace}><Icon name="chevronLeft" size={14} /> Back to Workspace</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => newFile()}><Icon name="plus" size={14} /> New file</DropdownMenuItem>
        <DropdownMenuItem data-testid="copy-canvas" disabled={!copyable} onSelect={() => setTimeout(onCopy)}><Icon name="duplicate" size={14} /> Save a copy…</DropdownMenuItem>
        <DropdownMenuItem data-testid="export-native-canvas" onSelect={() => setTimeout(onNativeExport)}><Icon name="export" size={14} /> Export native Canvas…</DropdownMenuItem>
        <DropdownMenuItem data-testid="import-native-canvas" onSelect={() => setTimeout(onNativeImport)}><Icon name="import" size={14} /> Import native Canvas…</DropdownMenuItem>
        {/* defer modal opens to the next tick — otherwise the menu-item pointerup that's still
            propagating is caught by the just-mounted dialog's dismiss layer and closes it instantly */}
        {foreignImporterAvailable && <DropdownMenuItem data-testid="import-pipeline" onSelect={() => setTimeout(onImport)}><Icon name="import" size={14} /> Import pipeline…</DropdownMenuItem>}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setJobsQuery('')}><Icon name="clock" size={14} /> <MenuDestination label="Jobs" detail="all Workspace work" /></DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setInboxQuery(inboxQuery)}><Icon name="note" size={14} /> <MenuDestination label="Inbox" detail="my terminal outcomes" /></DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setTimeout(onRunHistory)}><Icon name="clock" size={14} /> <MenuDestination label="Run history" detail="this Canvas audit trail" /></DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setTimeout(onVersionHistory)}><Icon name="refresh" size={14} /> Version history</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setTimeout(onSettings)}><Icon name="settings" size={14} /> Settings</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function MenuDestination({ label, detail }: { label: string; detail: string }) {
  return <span className="flex min-w-0 flex-1 items-baseline justify-between gap-3"><span>{label}</span><span aria-hidden className="truncate text-[10px] text-muted-foreground">{detail}</span></span>
}

function CanvasInboxIndicator({ count }: { count: number }) {
  const setInboxQuery = useStore((s) => s.setInboxQuery)
  const inboxQuery = useStore((s) => s.inboxQuery)
  const bounded = count > 99 ? '99+' : String(count)
  return <button type="button" data-testid="canvas-inbox-unread-badge" onClick={() => setInboxQuery(inboxQuery)}
    aria-label={`Inbox, ${count} unread outcomes`} title={`${count} Inbox outcome${count === 1 ? '' : 's'} need attention`}
    className="relative grid h-6 min-w-6 place-items-center rounded-full bg-foreground px-1 text-[10px] font-bold text-background hover:bg-foreground/85">
    {bounded}
  </button>
}

function FileMenu({ onCanvasSettings }: { onCanvasSettings: () => void }) {
  const [open, setOpen] = useState(false)
  const doc = useStore((s) => s.doc)
  const files = useStore((s) => s.files)
  const openFile = useStore((s) => s.openFile)
  const newFile = useStore((s) => s.newFile)
  const newFromExample = useStore((s) => s.newFromExample)
  const renameFile = useStore((s) => s.renameFile)
  const deleteFile = useStore((s) => s.deleteFile)
  const discardLocalDraft = useStore((s) => s.discardLocalDraft)
  const currentDraftId = useStore((s) => s.currentDraftId)
  const canvasRole = useStore((s) => s.canvasRole)
  const canEdit = roleCanEdit(canvasRole)
  const exampleIntent = useExampleCreationIntent(open && canEdit)
  const exampleCreatesSeparate = exampleIntent === 'create-separate'

  return (
    <DropdownMenu open={open} onOpenChange={setOpen} modal={false}>
      <DropdownMenuTrigger asChild>
        <button
          data-testid="file-menu"
          className="inline-flex cursor-pointer items-center gap-1 rounded-md border-0 bg-transparent px-1 py-0.5 text-[13.5px] font-semibold text-foreground"
        >
          {doc.name ?? 'untitled'} <span className="text-muted-foreground"><Icon name="chevronDown" size={12} /></span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[240px]">
        {/* stop keydown here so the menu's typeahead / arrow-nav doesn't steal focus from the rename box */}
        <div className="px-2 py-1.5" onKeyDown={(e) => e.stopPropagation()}>
          <input
            value={doc.name ?? ''}
            disabled={!canEdit}
            onChange={(e) => renameFile(e.target.value)}
            placeholder="untitled"
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-[12.5px] font-semibold text-foreground outline-none"
          />
        </div>
        <div className="px-2.5 py-1 text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">Files</div>
        <div className="max-h-[220px] overflow-y-auto">
          {files.map((f) => (
            <button
              key={f.id}
              onClick={() => { openFile(f.id); setOpen(false) }}
              className={cn(
                'flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-[12.5px] text-foreground hover:bg-accent',
                f.id === doc.id && 'bg-accent',
              )}
            >
              <span className="text-muted-foreground"><Icon name="grid" size={12} /></span>
              <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{f.name || 'untitled'}</span>
            </button>
          ))}
          {files.length === 0 && <div className="p-2.5 text-[11.5px] text-muted-foreground">No files yet.</div>}
        </div>
        <CanvasDraftMenu close={() => setOpen(false)} />
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setTimeout(onCanvasSettings)}><Icon name="settings" size={14} /> Canvas settings…</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => newFile()}><Icon name="plus" size={14} /> New file</DropdownMenuItem>
        <div className="px-2.5 pb-1 pt-1.5 text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">
          {exampleCreatesSeparate ? 'Create example Canvas' : 'New from example'}
        </div>
        {examples.map((ex) => (
          <DropdownMenuItem key={ex.key} onSelect={() => { setOpen(false); void newFromExample(ex.key, exampleIntent) }} title={ex.blurb}
            aria-label={exampleCreatesSeparate ? `Create example Canvas: ${ex.name}` : ex.name}>
            <Icon name="grid" size={14} /> {ex.name}
          </DropdownMenuItem>
        ))}
        {canvasRole === 'owner' && <DropdownMenuSeparator />}
        {canvasRole === 'owner' && <DropdownMenuItem onSelect={() => currentDraftId ? void discardLocalDraft(currentDraftId) : void deleteFile(doc.id)} className="text-destructive focus:text-destructive"><Icon name="trash" size={14} /> {currentDraftId ? 'Delete this local draft' : 'Delete this file'}</DropdownMenuItem>}
      </DropdownMenuContent>
    </DropdownMenu>
  )
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

function ThemeToggle() {
  const [dark, setDark] = useState(() => resolvedTheme() === 'dark')
  useEffect(() => {
    const sync = () => setDark(resolvedTheme() === 'dark')
    window.addEventListener('dp-theme-change', sync)
    const mql = window.matchMedia('(prefers-color-scheme: dark)')
    mql.addEventListener('change', sync)  // reflect OS changes while in 'system' mode
    return () => { window.removeEventListener('dp-theme-change', sync); mql.removeEventListener('change', sync) }
  }, [])
  // moon = "switch to dark" (shown in light); sun = "switch to light" (shown in dark)
  return <IconBtn name={dark ? 'sun' : 'moon'} label={dark ? 'Switch to light theme' : 'Switch to dark theme'} onClick={toggleTheme} />
}
