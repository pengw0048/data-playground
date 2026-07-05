import { useEffect, useState } from 'react'
import { useStore } from '../store/graph'
import { color } from '../theme/tokens'
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
import { RunHistoryModal } from '../panels/RunHistoryModal'
import { VersionHistoryModal } from '../panels/VersionHistoryModal'
import { ShareModal } from '../panels/ShareModal'
import { crdtUndoActive } from '../collab/undo'

export function TopBar() {
  const kernelUp = useStore((s) => s.kernelUp)
  const kernelInfo = useStore((s) => s.kernelInfo)
  const saved = useStore((s) => s.saved)
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

  // let anything (e.g. the agent's "Configure a model" CTA) open Settings
  useEffect(() => {
    const onOpen = () => setSettingsOpen(true)
    window.addEventListener('dp-open-settings', onOpen)
    return () => window.removeEventListener('dp-open-settings', onOpen)
  }, [])

  return (
    <>
      <div style={{ position: 'absolute', top: 16, left: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 8 }}>
        <AppMenu onSettings={() => setSettingsOpen(true)} onRunHistory={() => setRunsOpen(true)} onVersionHistory={() => setVersionsOpen(true)} />
        <span className="text-[13.5px] text-muted-foreground">/</span>
        <FileMenu onCanvasSettings={() => setCanvasSettingsOpen(true)} />
        <span data-testid="autosave" title={!kernelUp && saved ? 'Kernel offline — saved to this browser only' : undefined} className="ml-0.5 text-[11px] text-muted-foreground">· {saved ? (kernelUp ? 'saved' : 'saved locally') : 'saving…'}</span>
        <span className="ml-1.5 inline-flex gap-0.5">
          <IconBtn name="undo" label="Undo" disabled={!canUndo} onClick={() => useStore.getState().undo()} />
          <IconBtn name="redo" label="Redo" disabled={!canRedo} onClick={() => useStore.getState().redo()} />
        </span>
      </div>

      <div style={{ position: 'absolute', top: 16, right: 20, zIndex: 15, display: 'flex', alignItems: 'center', gap: 10 }}>
        <PeerAvatars />
        <div
          title={kernelInfo ? `${kernelInfo.backend} · ${kernelInfo.runners.join(', ')}` : 'kernel offline'}
          className="flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground shadow-sm"
        >
          <span className="h-2 w-2 rounded-full" style={{ background: kernelUp ? color.latest : color.failed }} />
          kernel · {kernelUp ? 'warm' : 'offline'}
        </div>
        <Button onClick={rerunAll} title="Re-run the whole graph" size="sm" className="rounded-full bg-foreground text-background hover:bg-foreground/90">
          <Icon name="refresh" size={13} /> Rerun all
        </Button>
        <Button data-testid="share-btn" onClick={() => setShareOpen(true)} title="Share this canvas" size="sm" className="rounded-full">
          <Icon name="link" size={13} /> Share
        </Button>
        {/* Settings lives in the app menu (top-left); identity + log out live on the files shell —
            no redundant Settings button / account avatar here. */}
      </div>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
      {canvasSettingsOpen && <CanvasSettingsModal onClose={() => setCanvasSettingsOpen(false)} />}
      {runsOpen && <RunHistoryModal onClose={() => setRunsOpen(false)} />}
      {versionsOpen && <VersionHistoryModal onClose={() => setVersionsOpen(false)} />}
      {shareOpen && <ShareModal onClose={() => setShareOpen(false)} />}
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

// The app menu (Figma-style hamburger): Back to files, New file, Run history, Version history, Settings.
function AppMenu({ onSettings, onRunHistory, onVersionHistory }: { onSettings: () => void; onRunHistory: () => void; onVersionHistory: () => void }) {
  const setView = useStore((s) => s.setView)
  const newFile = useStore((s) => s.newFile)
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button data-testid="app-menu" title="Menu"
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border-0 bg-transparent px-1 py-0.5 text-[13.5px] font-bold text-foreground">
          <span className="grid h-5 w-5 place-items-center rounded-[5px] bg-foreground text-xs font-bold text-background">D</span>
          <span className="text-muted-foreground"><Icon name="chevronDown" size={12} /></span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[210px]">
        <DropdownMenuItem onSelect={() => setView('files')}><Icon name="chevronLeft" size={14} /> Back to files</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => newFile()}><Icon name="plus" size={14} /> New file</DropdownMenuItem>
        {/* defer modal opens to the next tick — otherwise the menu-item pointerup that's still
            propagating is caught by the just-mounted dialog's dismiss layer and closes it instantly */}
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
  const renameFile = useStore((s) => s.renameFile)
  const deleteFile = useStore((s) => s.deleteFile)

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
        <DropdownMenuItem onSelect={() => deleteFile(doc.id)} className="text-destructive focus:text-destructive"><Icon name="trash" size={14} /> Delete this file</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function IconBtn({ name, label, onClick, disabled }: { name: IconName; label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <Button variant="ghost" size="icon" aria-label={label} title={label} onClick={onClick} disabled={disabled} className="h-7 w-7 text-muted-foreground">
      <Icon name={name} size={14} />
    </Button>
  )
}
