import { useEffect, useState } from 'react'
import { api, type ShareInfo } from '../api/client'
import { useStore } from '../store/graph'
import { canvasLink } from '../router'
import { Icon } from '../ui/Icon'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

const sectionLabel = 'text-[9.5px] font-bold uppercase tracking-[0.5px] text-muted-foreground mb-1.5'
// The role/collaborator pickers stay native <select> on purpose: the E2E suite drives them with
// selectOption() and asserts on <option value="viewer">, which Radix's Select does not produce.
const nativeSelect =
  'h-8 rounded-md border border-input bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring'

// Share a canvas: workspace visibility + explicit collaborators (owner-only). Mirrors Figma's Share.
export function ShareModal({ onClose }: { onClose: () => void }) {
  const canvasId = useStore((s) => s.doc.id)
  const users = useStore((s) => s.users)
  const currentUser = useStore((s) => s.currentUser)
  const pushToast = useStore((s) => s.pushToast)
  const [visibility, setVisibility] = useState('private')
  const [shares, setShares] = useState<ShareInfo[]>([])
  const [pick, setPick] = useState('')
  const [role, setRole] = useState<'editor' | 'viewer'>('editor')

  const load = () => api.getShares(canvasId).then((r) => { setVisibility(r.visibility); setShares(r.shares) }).catch(() => {})
  useEffect(() => { load() }, [canvasId])

  const setVis = async (v: string) => { setVisibility(v); await api.addShare(canvasId, { visibility: v }).catch((e) => pushToast((e as Error).message, 'error')) }
  const add = async () => {
    if (!pick) return
    await api.addShare(canvasId, { userId: pick, role }).catch((e) => pushToast((e as Error).message, 'error'))
    setPick(''); load()
  }
  const changeRole = async (userId: string, r: string) => { await api.addShare(canvasId, { userId, role: r }).catch((e) => pushToast((e as Error).message, 'error')); load() }
  const remove = async (userId: string) => { await api.removeShare(canvasId, userId).catch(() => {}); load() }

  const sharedIds = new Set([currentUser?.id, ...shares.map((s) => s.userId)])
  const addable = users.filter((u) => !sharedIds.has(u.id))

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="dp-modal-overlay flex w-[460px] max-w-[calc(100vw-2rem)] flex-col gap-0 overflow-hidden p-0 [&>button]:hidden">
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <span className="text-muted-foreground"><Icon name="link" size={15} /></span>
          <DialogTitle className="text-sm font-semibold text-foreground">Share this canvas</DialogTitle>
          <span className="flex-1" />
          <button onClick={onClose} aria-label="Close" className="cursor-pointer border-0 bg-transparent p-0 text-muted-foreground hover:text-foreground"><Icon name="close" size={16} /></button>
        </div>
        <div className="flex flex-col gap-3.5 p-4">
          <div>
            <div className={sectionLabel}>Link</div>
            <div className="flex gap-1.5">
              <Input readOnly value={canvasLink(canvasId)} onClick={(e) => (e.target as HTMLInputElement).select()}
                className="h-8 flex-1 bg-muted text-[11.5px] text-muted-foreground" />
              <Button data-testid="copy-link" type="button" variant="outline" size="sm"
                onClick={() => { navigator.clipboard?.writeText(canvasLink(canvasId)).then(() => pushToast('Link copied', 'success'), () => {}) }}>Copy</Button>
            </div>
            <div className="mt-1.5 text-[10.5px] text-muted-foreground">Opens this canvas directly. People need at least workspace access (or an explicit invite below).</div>
          </div>
          <div>
            <div className={sectionLabel}>Visibility</div>
            <div className="inline-flex flex-wrap gap-[3px] rounded-md bg-muted p-0.5">
              {(['private', 'workspace', 'workspace_view'] as const).map((v) => (
                <Button key={v} type="button" size="sm" variant={visibility === v ? 'default' : 'ghost'} onClick={() => setVis(v)}
                  className="h-7 px-3 text-[11.5px]">
                  {v === 'private' ? 'Private' : v === 'workspace' ? 'Everyone in workspace' : 'Everyone in workspace (view-only)'}
                </Button>
              ))}
            </div>
          </div>
          <div>
            <div className={sectionLabel}>Collaborators</div>
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center gap-2 text-[12.5px] text-muted-foreground">
                <span className="grid h-[22px] w-[22px] place-items-center rounded-full bg-primary/10 text-[10px] font-bold text-primary">{(currentUser?.name ?? '?').slice(0, 1).toUpperCase()}</span>
                {currentUser?.name ?? 'you'} <span className="text-muted-foreground">· owner</span>
              </div>
              {shares.map((sh) => (
                <div key={sh.userId} className="flex items-center gap-2 text-[12.5px] text-foreground">
                  <span className="grid h-[22px] w-[22px] place-items-center rounded-full bg-muted text-[10px] font-bold text-muted-foreground">{sh.name.slice(0, 1).toUpperCase()}</span>
                  <span className="flex-1">{sh.name}</span>
                  <select value={sh.role} onChange={(e) => changeRole(sh.userId, e.target.value)} title="Access level" className={nativeSelect}>
                    <option value="editor">can edit</option>
                    <option value="viewer">can view</option>
                  </select>
                  <button onClick={() => remove(sh.userId)} title="Remove" className="cursor-pointer border-0 bg-transparent p-0 text-muted-foreground hover:text-foreground"><Icon name="close" size={13} /></button>
                </div>
              ))}
            </div>
            {addable.length > 0 && (
              <div className="mt-2.5 flex gap-1.5">
                <select value={pick} onChange={(e) => setPick(e.target.value)} className={cn(nativeSelect, 'flex-1')}>
                  <option value="">Add a collaborator…</option>
                  {addable.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
                </select>
                <select value={role} onChange={(e) => setRole(e.target.value as 'editor' | 'viewer')} title="Access level" className={nativeSelect}>
                  <option value="editor">can edit</option>
                  <option value="viewer">can view</option>
                </select>
                <Button type="button" size="sm" onClick={add} disabled={!pick}>Add</Button>
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
