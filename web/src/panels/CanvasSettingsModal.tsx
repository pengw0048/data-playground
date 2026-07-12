import { useEffect, useState } from 'react'
import { type CanvasVisibility } from '../api/client'
import { roleCanEdit, useStore } from '../store/graph'
import { Icon } from '../ui/Icon'
import { useCanvasSharing } from './useCanvasSharing'
import { cn } from '@/lib/utils'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

const VISIBILITIES: { value: CanvasVisibility; title: string; description: string }[] = [
  { value: 'private', title: 'Private', description: 'Only the owner and invited people' },
  { value: 'workspace', title: 'Workspace', description: 'Everyone in the workspace can edit' },
  { value: 'workspace_view', title: 'Workspace view-only', description: 'Everyone in the workspace can view' },
]

// Settings scoped to THIS canvas (not the app/workspace ones). Sharing state comes from the same hook
// as ShareModal so both surfaces have identical pending, failure, and retry behavior.
export function CanvasSettingsModal({ onClose }: { onClose: () => void }) {
  const doc = useStore((s) => s.doc)
  const canvasRole = useStore((s) => s.canvasRole)
  const renameFile = useStore((s) => s.renameFile)
  const setRequirements = useStore((s) => s.setRequirements)
  const canEdit = roleCanEdit(canvasRole)
  const isOwner = canvasRole === 'owner'
  const sharing = useCanvasSharing(doc.id, isOwner)
  const [name, setName] = useState(doc.name ?? '')
  const [reqs, setReqs] = useState((doc.requirements ?? []).join('\n'))

  useEffect(() => {
    setName(doc.name ?? '')
    setReqs((doc.requirements ?? []).join('\n'))
  }, [doc.id, doc.name, doc.requirements])

  const busy = sharing.pending !== null
  const access = canvasRole === 'owner'
    ? 'Owner access'
    : canvasRole === 'editor'
      ? 'Editor access'
      : canvasRole === 'viewer'
        ? 'View-only access'
        : 'Access is unknown — editing is disabled'

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="dp-modal-overlay w-[440px] max-w-[92vw] gap-0 overflow-hidden rounded-xl p-0">
        <div className="flex items-center gap-2 border-b border-border py-3 pl-4 pr-12">
          <span className="flex items-center text-muted-foreground"><Icon name="grid" size={14} /></span>
          <DialogTitle className="text-sm font-semibold">Canvas settings</DialogTitle>
        </div>
        <DialogDescription className="sr-only">Settings for the current canvas: its name, visibility, and dependencies.</DialogDescription>

        <div className="flex flex-col gap-4 p-4">
          <div className="rounded-md bg-muted px-2.5 py-1.5 text-[10.5px] text-muted-foreground">{access}</div>
          {sharing.error && (
            <div role="alert" className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-2.5 py-2 text-[11.5px] text-destructive">
              <span className="min-w-0 flex-1">{sharing.error}</span>
              {sharing.retryable && <Button type="button" variant="outline" size="sm" onClick={sharing.retry} disabled={busy} className="h-6 px-2 text-[10.5px]">Retry</Button>}
            </div>
          )}
          {sharing.pending && sharing.pending !== 'load' && (
            <div role="status" className="text-[10.5px] text-muted-foreground">Saving sharing changes…</div>
          )}
          <div>
            <Label className="mb-1 block text-[11.5px] font-normal text-muted-foreground">Name</Label>
            <Input value={name} disabled={!canEdit} onChange={(event) => { setName(event.target.value); renameFile(event.target.value) }} placeholder="untitled" />
          </div>
          <div>
            <div className="mb-1.5 text-[11.5px] text-muted-foreground">Visibility</div>
            {sharing.visibility === null && sharing.pending === 'load' ? (
              <div className="text-[11.5px] text-muted-foreground">Loading visibility…</div>
            ) : (
              <div className="grid gap-2">
                {VISIBILITIES.map(({ value, title, description }) => (
                  <button key={value} onClick={() => sharing.setCanvasVisibility(value)} disabled={!isOwner || busy || sharing.visibility === null}
                    aria-pressed={sharing.visibility === value}
                    className={cn('rounded-lg border px-2.5 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-70',
                      sharing.visibility === value ? 'border-primary bg-primary/10' : 'border-border bg-background enabled:hover:bg-accent/50')}>
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
                      <Icon name={value === 'private' ? 'grid' : 'link'} size={12} /> {title}
                    </div>
                    <div className="mt-0.5 text-[10.5px] font-normal text-muted-foreground">{description}</div>
                  </button>
                ))}
              </div>
            )}
            <div className="mt-2 text-[10.5px] text-muted-foreground">
              {isOwner ? <>Invite specific people from the <b>Share</b> button.</> : 'Only the canvas owner can change visibility.'}
            </div>
          </div>
          <div>
            <Label className="mb-1 block text-[11.5px] font-normal text-muted-foreground">Dependencies (pip)</Label>
            <textarea
              value={reqs}
              disabled={!canEdit}
              onChange={(event) => {
                setReqs(event.target.value)
                setRequirements(event.target.value.split('\n').map((value) => value.trim()).filter(Boolean))
              }}
              placeholder={'pandas\nscikit-learn==1.5'}
              spellCheck={false}
              rows={3}
              className="dp-mono w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-[11.5px] text-foreground outline-none disabled:cursor-not-allowed disabled:opacity-70"
            />
            <div className="mt-1 text-[10.5px] text-muted-foreground">One pip spec per line — installed on this canvas's kernel, then importable in <code>transform</code> cells. Travels with the canvas.</div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
