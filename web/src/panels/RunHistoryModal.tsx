import { useEffect, useState } from 'react'
import { api, type RunRecordDto } from '../api/client'
import { useStore } from '../store/graph'
import { status as statusTok } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'

// Persisted run history for the current canvas (survives restarts) — /canvas/{id}/runs.
export function RunHistoryModal({ onClose }: { onClose: () => void }) {
  const canvasId = useStore((s) => s.doc.id)
  const [runs, setRuns] = useState<RunRecordDto[] | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    api.listRuns(canvasId).then(setRuns).catch((e) => setErr((e as Error).message))
  }, [canvasId])

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="dp-modal-overlay flex max-h-[76vh] w-[560px] max-w-[calc(100vw-2rem)] flex-col gap-0 overflow-hidden p-0 [&>button]:hidden">
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <span className="text-muted-foreground"><Icon name="clock" size={15} /></span>
          <DialogTitle className="text-sm font-semibold text-foreground">Run history</DialogTitle>
          <span className="flex-1" />
          <button onClick={onClose} aria-label="Close" className="cursor-pointer border-0 bg-transparent p-0 text-muted-foreground hover:text-foreground"><Icon name="close" size={16} /></button>
        </div>
        <div className="overflow-y-auto p-2">
          {err && <div className="p-4 text-[12.5px] text-destructive">Couldn’t load run history: {err}</div>}
          {!err && runs === null && <div className="p-4 text-[12.5px] text-muted-foreground">Loading…</div>}
          {!err && runs?.length === 0 && <div className="p-4 text-[12.5px] text-muted-foreground">No runs yet — run a pipeline and it’ll show here.</div>}
          {runs?.map((r) => {
            const st = statusTok[r.status as keyof typeof statusTok] ?? statusTok.draft
            return (
              <div key={r.id} className="flex items-center gap-2.5 border-b border-border px-2.5 py-2 text-[12.5px]">
                <span className="w-3 text-center" style={{ color: st.color }}>{st.glyph}</span>
                <Badge variant="secondary" className="w-[70px] justify-center">{r.status}</Badge>
                <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-foreground">
                  {r.outputTable ? `→ ${r.outputTable}` : r.targetNodeId ?? '—'}
                  {r.error && <span className="text-destructive"> · {r.error}</span>}
                </span>
                {r.rows != null && <span className="text-muted-foreground">{r.rows.toLocaleString()} rows</span>}
                {r.ms != null && <span className="w-14 text-right text-muted-foreground">{r.ms} ms</span>}
                <span className="w-32 text-right text-[11px] text-muted-foreground">{r.createdAt ? new Date(r.createdAt).toLocaleString() : ''}</span>
              </div>
            )
          })}
        </div>
      </DialogContent>
    </Dialog>
  )
}
