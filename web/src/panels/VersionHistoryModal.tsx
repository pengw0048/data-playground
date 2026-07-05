import { useEffect, useState } from 'react'
import { api, type CanvasVersionDto } from '../api/client'
import { useStore } from '../store/graph'
import { Icon } from '../ui/Icon'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

// Server-side snapshot history for the current canvas (/canvas/{id}/versions) with one-click restore.
// Snapshots are captured (throttled) on every save, so a bad edit is recoverable after the fact.
export function VersionHistoryModal({ onClose }: { onClose: () => void }) {
  const canvasId = useStore((s) => s.doc.id)
  const loadDoc = useStore((s) => s.loadDoc)
  const pushToast = useStore((s) => s.pushToast)
  const [versions, setVersions] = useState<CanvasVersionDto[] | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')

  const load = () => api.listVersions(canvasId).then(setVersions).catch((e) => setErr((e as Error).message))
  useEffect(() => { load() }, [canvasId])

  const restore = async (v: CanvasVersionDto) => {
    setBusy(v.id)
    try {
      const r = await api.restoreCanvas(canvasId, v.id)
      loadDoc(r.doc)            // swap the canvas to the restored state (also snapshots the pre-restore state)
      pushToast('Restored an earlier version', 'success')
      onClose()
    } catch (e) {
      pushToast((e as Error).message || 'Restore failed', 'error')
    } finally {
      setBusy('')
    }
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="dp-modal-overlay flex max-h-[76vh] w-[560px] max-w-[calc(100vw-2rem)] flex-col gap-0 overflow-hidden p-0 [&>button]:hidden">
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <span className="text-muted-foreground"><Icon name="clock" size={15} /></span>
          <DialogTitle className="text-sm font-semibold text-foreground">Version history</DialogTitle>
          <span className="flex-1" />
          <button onClick={onClose} aria-label="Close" className="cursor-pointer border-0 bg-transparent p-0 text-muted-foreground hover:text-foreground"><Icon name="close" size={16} /></button>
        </div>
        <div className="overflow-y-auto p-2">
          {err && <div className="p-4 text-[12.5px] text-destructive">Couldn’t load history: {err}</div>}
          {!err && versions === null && <div className="p-4 text-[12.5px] text-muted-foreground">Loading…</div>}
          {!err && versions?.length === 0 && <div className="p-4 text-[12.5px] text-muted-foreground">No snapshots yet — they’re captured as you edit.</div>}
          {versions?.map((v) => (
            <div key={v.id} className="flex items-center gap-2.5 border-b border-border px-2.5 py-2 text-[12.5px]">
              <span className={v.label ? 'text-primary' : 'text-muted-foreground'}><Icon name={v.label ? 'refresh' : 'clock'} size={13} /></span>
              <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-foreground">
                {v.label ?? `Snapshot · v${v.version}`}
              </span>
              <span className="text-[11px] text-muted-foreground">{v.createdAt ? new Date(v.createdAt).toLocaleString() : ''}</span>
              <Button type="button" variant="outline" size="sm" onClick={() => restore(v)} disabled={!!busy}
                className={cn('text-[11.5px]', busy === v.id && 'disabled:opacity-100')}>
                {busy === v.id ? 'Restoring…' : 'Restore'}
              </Button>
            </div>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}
