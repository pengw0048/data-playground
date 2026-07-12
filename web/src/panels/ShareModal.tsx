import { useEffect, useState } from 'react'
import { type CanvasVisibility, type ShareRole } from '../api/client'
import { useStore } from '../store/graph'
import { canvasLink } from '../router'
import { Icon } from '../ui/Icon'
import { useCanvasSharing } from './useCanvasSharing'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

const sectionLabel = 'text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground mb-1.5'
// The role/collaborator pickers stay native <select> on purpose: the E2E suite drives them with
// selectOption() and asserts on <option value="viewer">, which Radix's Select does not produce.
const nativeSelect =
  'h-8 rounded-md border border-input bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-60'

const VISIBILITIES: { value: CanvasVisibility; label: string }[] = [
  { value: 'private', label: 'Private' },
  { value: 'workspace', label: 'Everyone in workspace' },
  { value: 'workspace_view', label: 'Everyone in workspace (view-only)' },
]

// Share a canvas: workspace visibility + explicit collaborators. Everyone with access can inspect
// the current policy and copy the link; only the server-reported owner gets mutation controls.
export function ShareModal({ onClose }: { onClose: () => void }) {
  const canvasId = useStore((s) => s.doc.id)
  const canvasRole = useStore((s) => s.canvasRole)
  const users = useStore((s) => s.users)
  const currentUser = useStore((s) => s.currentUser)
  const pushToast = useStore((s) => s.pushToast)
  const isOwner = canvasRole === 'owner'
  const sharing = useCanvasSharing(canvasId, isOwner)
  const [pick, setPick] = useState('')
  const [role, setRole] = useState<ShareRole>('editor')

  const visibleShares = sharing.shares.filter((share) => share.userId !== currentUser?.id)
  const sharedIds = new Set([currentUser?.id, ...visibleShares.map((share) => share.userId)])
  const addable = users.filter((user) => !sharedIds.has(user.id))
  const busy = sharing.pending !== null
  const roleLabel = canvasRole === 'owner'
    ? 'owner'
    : canvasRole === 'editor'
      ? 'can edit'
      : canvasRole === 'viewer'
        ? 'can view'
        : 'access unknown'

  // A successful generic Retry lives in the shared hook, outside this component's add() call. Clear
  // the picker once that user appears in the confirmed share list; failed attempts keep it intact.
  useEffect(() => {
    if (pick && sharing.shares.some((share) => share.userId === pick)) setPick('')
  }, [pick, sharing.shares])

  const add = async () => {
    const user = users.find((candidate) => candidate.id === pick)
    if (!user) return
    if (await sharing.addCollaborator(user, role)) setPick('')
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="dp-modal-overlay flex w-[460px] max-w-[calc(100vw-2rem)] flex-col gap-0 overflow-hidden p-0 [&>button]:hidden">
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <span className="text-muted-foreground"><Icon name="link" size={15} /></span>
          <DialogTitle className="text-sm font-semibold text-foreground">Share this canvas</DialogTitle>
          <span className="flex-1" />
          <button onClick={onClose} aria-label="Close" className="cursor-pointer border-0 bg-transparent p-0 text-muted-foreground hover:text-foreground"><Icon name="close" size={16} /></button>
        </div>
        <div className="flex flex-col gap-3.5 p-4">
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
            <div className={sectionLabel}>Link</div>
            <div className="flex gap-1.5">
              <Input readOnly value={canvasLink(canvasId)} onClick={(event) => (event.target as HTMLInputElement).select()}
                className="h-8 flex-1 bg-muted text-[11.5px] text-muted-foreground" />
              <Button data-testid="copy-link" type="button" variant="outline" size="sm"
                onClick={() => {
                  navigator.clipboard?.writeText(canvasLink(canvasId)).then(
                    () => pushToast('Link copied', 'success'),
                    () => pushToast('Could not copy the link', 'error'),
                  )
                }}>Copy</Button>
            </div>
            <div className="mt-1.5 text-[10.5px] text-muted-foreground">Opens this canvas directly. People need workspace access or an explicit invite below.</div>
          </div>
          <div>
            <div className={sectionLabel}>Visibility</div>
            {sharing.visibility === null && sharing.pending === 'load' ? (
              <div className="text-[11.5px] text-muted-foreground">Loading visibility…</div>
            ) : (
              <div className="inline-flex flex-wrap gap-[3px] rounded-md bg-muted p-0.5">
                {VISIBILITIES.map(({ value, label }) => (
                  <Button key={value} type="button" size="sm" variant={sharing.visibility === value ? 'default' : 'ghost'}
                    aria-pressed={sharing.visibility === value}
                    onClick={() => sharing.setCanvasVisibility(value)} disabled={!isOwner || busy || sharing.visibility === null}
                    className="h-7 px-3 text-[11.5px]">
                    {label}
                  </Button>
                ))}
              </div>
            )}
            {!isOwner && <div className="mt-1.5 text-[10.5px] text-muted-foreground">Only the canvas owner can change visibility and collaborators.</div>}
          </div>
          <div>
            <div className={sectionLabel}>Collaborators</div>
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center gap-2 text-[12.5px] text-muted-foreground">
                <span className="grid h-[22px] w-[22px] place-items-center rounded-full bg-primary/10 text-[10px] font-bold text-primary">{(currentUser?.name ?? '?').slice(0, 1).toUpperCase()}</span>
                {currentUser?.name ?? 'you'} <span className="text-muted-foreground">· {roleLabel}</span>
              </div>
              {visibleShares.map((share) => (
                <div key={share.userId} className="flex items-center gap-2 text-[12.5px] text-foreground">
                  <span className="grid h-[22px] w-[22px] place-items-center rounded-full bg-muted text-[10px] font-bold text-muted-foreground">{share.name.slice(0, 1).toUpperCase()}</span>
                  <span className="flex-1">{share.name}</span>
                  {isOwner ? (
                    <>
                      <select value={share.role} onChange={(event) => sharing.changeCollaboratorRole(share.userId, event.target.value as ShareRole)}
                        disabled={busy} title="Access level" aria-label={`Access level for ${share.name}`} className={nativeSelect}>
                        <option value="editor">can edit</option>
                        <option value="viewer">can view</option>
                      </select>
                      <button onClick={() => sharing.removeCollaborator(share.userId)} disabled={busy} title="Remove"
                        className="cursor-pointer border-0 bg-transparent p-0 text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"><Icon name="close" size={13} /></button>
                    </>
                  ) : <span className="text-[11.5px] text-muted-foreground">{share.role === 'editor' ? 'can edit' : 'can view'}</span>}
                </div>
              ))}
            </div>
            {isOwner && addable.length > 0 && (
              <div className="mt-2.5 flex gap-1.5">
                <select value={pick} onChange={(event) => setPick(event.target.value)} disabled={busy} aria-label="Collaborator" className={cn(nativeSelect, 'flex-1')}>
                  <option value="">Add a collaborator…</option>
                  {addable.map((user) => <option key={user.id} value={user.id}>{user.name}</option>)}
                </select>
                <select value={role} onChange={(event) => setRole(event.target.value as ShareRole)} disabled={busy} title="Access level" aria-label="New collaborator access" className={nativeSelect}>
                  <option value="editor">can edit</option>
                  <option value="viewer">can view</option>
                </select>
                <Button type="button" size="sm" onClick={add} disabled={!pick || busy}>
                  {sharing.pending?.startsWith('add:') ? 'Adding…' : 'Add'}
                </Button>
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
