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

export function TopBar() {
  const kernelUp = useStore((s) => s.kernelUp)
  const kernelInfo = useStore((s) => s.kernelInfo)
  const saved = useStore((s) => s.saved)
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
  const settingsTrigger = useRef<HTMLElement | null>(null)
  const saveLabel = !canEdit
    ? (canvasRole === 'viewer' ? 'view only' : 'read only')
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

  const openSettings = (trigger: HTMLElement) => {
    settingsTrigger.current = trigger
    setSettingsOpen(true)
  }
  const closeSettings = () => {
    setSettingsOpen(false)
    requestAnimationFrame(() => settingsTrigger.current?.focus())
  }

  return (
    <>
      <div style={{ position: 'absolute', top: 16, left: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 8 }}>
        <AppMenu onSettings={() => openSettings(document.querySelector<HTMLElement>('[data-testid="app-menu"]')!)} onRunHistory={() => setRunsOpen(true)} onVersionHistory={() => setVersionsOpen(true)} onImport={() => setImportOpen(true)} />
        <span className="text-[13.5px] text-muted-foreground">/</span>
        <FileMenu onCanvasSettings={() => setCanvasSettingsOpen(true)} />
        <span data-testid="autosave" title={!canEdit ? 'Editing is disabled for your current access level' : !kernelUp && saved ? 'Kernel offline — saved to this browser only' : undefined} className="ml-0.5 text-[11px] text-muted-foreground">· {saveLabel}</span>
        <span className="ml-1.5 inline-flex gap-0.5">
          <IconBtn name="undo" label="Undo" disabled={!canEdit || !canUndo} onClick={() => useStore.getState().undo()} />
          <IconBtn name="redo" label="Redo" disabled={!canEdit || !canRedo} onClick={() => useStore.getState().redo()} />
          <ThemeToggle />
        </span>
      </div>

      <div style={{ position: 'absolute', top: 16, right: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 10 }}>
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
function AppMenu({ onSettings, onRunHistory, onVersionHistory, onImport }: { onSettings: () => void; onRunHistory: () => void; onVersionHistory: () => void; onImport: () => void }) {
  const setView = useStore((s) => s.setView)
  const newFile = useStore((s) => s.newFile)
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
        <DropdownMenuItem onSelect={() => setView('workspace')}><Icon name="chevronLeft" size={14} /> Back to Workspace</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => newFile()}><Icon name="plus" size={14} /> New file</DropdownMenuItem>
        {/* defer modal opens to the next tick — otherwise the menu-item pointerup that's still
            propagating is caught by the just-mounted dialog's dismiss layer and closes it instantly */}
        <DropdownMenuItem data-testid="import-pipeline" onSelect={() => setTimeout(onImport)}><Icon name="import" size={14} /> Import pipeline…</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setTimeout(onRunHistory)}><Icon name="clock" size={14} /> Run history</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setTimeout(onVersionHistory)}><Icon name="refresh" size={14} /> Version history</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setTimeout(onSettings)}><Icon name="settings" size={14} /> Settings</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
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
  const canvasRole = useStore((s) => s.canvasRole)
  const canEdit = roleCanEdit(canvasRole)

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
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setTimeout(onCanvasSettings)}><Icon name="settings" size={14} /> Canvas settings…</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => newFile()}><Icon name="plus" size={14} /> New file</DropdownMenuItem>
        <div className="px-2.5 pb-1 pt-1.5 text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground">New from example</div>
        {examples.map((ex) => (
          <DropdownMenuItem key={ex.key} onSelect={() => newFromExample(ex.key)} title={ex.blurb}>
            <Icon name="grid" size={14} /> {ex.name}
          </DropdownMenuItem>
        ))}
        {canvasRole === 'owner' && <DropdownMenuSeparator />}
        {canvasRole === 'owner' && <DropdownMenuItem onSelect={() => deleteFile(doc.id)} className="text-destructive focus:text-destructive"><Icon name="trash" size={14} /> Delete this file</DropdownMenuItem>}
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
