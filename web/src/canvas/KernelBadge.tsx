import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { CanvasKernelStatus, KernelInfo } from '../types/api'
import { roleCanEdit, useStore } from '../store/graph'
import { color } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function badgeLabel(kernelUp: boolean, status: CanvasKernelStatus | null): string {
  if (!kernelUp) return 'offline'
  if (!status?.exists) return 'cold'
  if (status.stale) return 'stale'
  const cache = status.relationCache
  if (cache && cache.entries > 0) return `warm · ${cache.entries} cached · ${fmtBytes(cache.bytes)}`
  return status.state === 'ready' ? 'warm' : (status.state ?? 'warm')
}

export function KernelBadge({ kernelUp, kernelInfo }: { kernelUp: boolean; kernelInfo: KernelInfo | null }) {
  const canvasId = useStore((s) => s.doc.id)
  const canvasRole = useStore((s) => s.canvasRole)
  const pushToast = useStore((s) => s.pushToast)
  const canEdit = roleCanEdit(canvasRole)
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<CanvasKernelStatus | null>(null)
  const [restarting, setRestarting] = useState(false)

  const refresh = useCallback(async () => {
    if (!canvasId) return
    try {
      setStatus(await api.kernelState(canvasId))
    } catch {
      setStatus({ exists: false })
    }
  }, [canvasId])

  useEffect(() => {
    if (!kernelUp || !canvasId) { setStatus(null); return }
    refresh()
    const id = window.setInterval(refresh, open ? 3_000 : 15_000)
    return () => window.clearInterval(id)
  }, [kernelUp, canvasId, open, refresh])

  const label = badgeLabel(kernelUp, status)
  const dotColor = !kernelUp ? color.failed : status?.stale ? color.running : color.latest

  const restart = async () => {
    if (!canEdit || !canvasId) return
    setRestarting(true)
    try {
      const r = await api.restartKernel(canvasId)
      pushToast(r.restarted ? 'Kernel restarting…' : 'No live kernel — a fresh one starts on the next run', 'success')
      await refresh()
    } catch (e) {
      pushToast((e as Error).message, 'error')
    } finally {
      setRestarting(false)
    }
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          data-testid="kernel-badge"
          type="button"
          aria-label="Kernel status"
          className="flex cursor-pointer items-center gap-2 rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground shadow-sm transition-colors hover:bg-accent hover:text-foreground"
        >
          <span className="h-2 w-2 rounded-full" style={{ background: dotColor }} />
          kernel · {label}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-3">
        <div className="mb-2 text-xs font-semibold text-foreground">Execution kernel</div>
        {!kernelUp ? (
          <p className="text-[11.5px] text-muted-foreground">Hub unreachable — saved locally until it reconnects.</p>
        ) : (
          <div className="flex flex-col gap-2 text-[11.5px]">
            <Row label="State">
              <span className={cn('font-medium capitalize', status?.stale && 'text-amber-600')}>
                {!status?.exists ? 'cold (starts on next run)' : status.stale ? 'stale' : (status.state ?? 'ready')}
              </span>
            </Row>
            {kernelInfo && (
              <>
                <Row label="Backend"><span className="font-mono text-[10.5px]">{kernelInfo.backend}</span></Row>
                <Row label="Runners"><span className="font-mono text-[10.5px]">{kernelInfo.runners.join(', ')}</span></Row>
              </>
            )}
            {status?.memoryLimit && <Row label="Memory limit"><span>{status.memoryLimit}</span></Row>}
            {status?.relationCache && (
              <Row label="Relation cache">
                <span>{status.relationCache.entries} entries · {fmtBytes(status.relationCache.bytes)}</span>
              </Row>
            )}
            {(status?.inflight ?? 0) > 0 && <Row label="In flight"><span>{status!.inflight} preview(s)</span></Row>}
            {(status?.activeRuns ?? 0) > 0 && <Row label="Active runs"><span>{status!.activeRuns}</span></Row>}
            <Button
              variant="outline"
              size="sm"
              className="mt-1 w-full"
              disabled={!canEdit || restarting}
              title={canEdit ? 'Clear warm state and restart the kernel' : 'View-only canvas'}
              onClick={restart}
            >
              <Icon name="refresh" size={13} /> {restarting ? 'Restarting…' : 'Restart kernel'}
            </Button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right text-foreground">{children}</span>
    </div>
  )
}
