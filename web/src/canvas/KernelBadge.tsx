import { type ReactNode, useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { CanvasKernelStatus, KernelInfo } from '../types/api'
import { roleCanEdit, useStore } from '../store/graph'
import { color } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

function fmtUptime(s: number): string {
  const sec = Math.floor(s)
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`
}

type Category = 'offline' | 'cold' | 'stale' | 'warm'

// A truly-absent lease (cold, muted) is NOT the same as a transient failure (offline, red): a failed
// poll keeps the last-known status and only flips the dot to offline — it never fabricates 'cold'.
function category(kernelUp: boolean, offline: boolean, status: CanvasKernelStatus | null): Category {
  if (!kernelUp || offline) return 'offline'
  if (!status || !status.exists) return 'cold'
  if (status.stale) return 'stale'
  return 'warm'
}

const DOT: Record<Category, string> = {
  offline: color.failed,   // red — hub/kernel unreachable
  cold: color.queued,      // muted — no live kernel (spawns on next run)
  stale: color.stale,      // amber — lease heartbeat gone stale
  warm: color.latest,      // green — live + healthy
}

export function KernelBadge({ kernelUp, kernelInfo }: { kernelUp: boolean; kernelInfo: KernelInfo | null }) {
  const canvasId = useStore((s) => s.doc.id)
  const canvasRole = useStore((s) => s.canvasRole)
  const pushToast = useStore((s) => s.pushToast)
  const canEdit = roleCanEdit(canvasRole)
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<CanvasKernelStatus | null>(null)
  const [offline, setOffline] = useState(false)
  const [restarting, setRestarting] = useState(false)

  const refresh = useCallback(async () => {
    if (!canvasId) return
    try {
      setStatus(await api.kernelState(canvasId))
      setOffline(false)
    } catch {
      setOffline(true)  // transient/unreachable — keep last-known status, don't fabricate 'cold'
    }
  }, [canvasId])

  // Fetch live status only while the popover is OPEN, then poll every 3s (the last status is cached, so
  // a reopened badge shows the previous state instantly). A closed badge issues NO request — this keeps
  // a freshly-loaded canvas (whose client-side id may not exist server-side yet) from querying the
  // kernel endpoint and logging a 404 console error, and adds no steady-state load.
  useEffect(() => {
    if (!open || !canvasId || !canvasRole) return
    void refresh()
    const id = window.setInterval(() => void refresh(), 3_000)
    return () => window.clearInterval(id)
  }, [open, canvasId, canvasRole, refresh])

  const cat = category(kernelUp, offline, status)

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

  const cache = status?.relationCache
  const rss = status?.memoryRssBytes
  const memText = rss != null
    ? (status?.memoryLimit ? `${fmtBytes(rss)} / ${status.memoryLimit}` : fmtBytes(rss))
    : (status?.memoryLimit ? `${status.memoryLimit} limit` : null)
  const stateText = cat === 'offline' ? 'offline'
    : cat === 'cold' ? 'cold (starts on next run)'
      : cat === 'stale' ? 'stale' : (status?.state ?? 'ready')

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          data-testid="kernel-badge"
          type="button"
          aria-label="Kernel status"
          className="flex cursor-pointer items-center gap-2 rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground shadow-sm transition-colors hover:bg-accent hover:text-foreground"
        >
          <span className="h-2 w-2 rounded-full" style={{ background: DOT[cat] }} />
          kernel · {cat}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-3">
        <div className="mb-2 text-xs font-semibold text-foreground">Execution kernel</div>
        <div className="flex flex-col gap-2 text-[11.5px]">
          <Row label="State"><span className="font-medium capitalize" style={{ color: DOT[cat] }}>{stateText}</span></Row>
          {kernelInfo && (
            <>
              <Row label="Backend"><span className="font-mono text-[10.5px]">{kernelInfo.backend}</span></Row>
              <Row label="Runners"><span className="font-mono text-[10.5px]">{kernelInfo.runners.join(', ')}</span></Row>
            </>
          )}
          {status?.uptimeSeconds != null && <Row label="Uptime"><span>{fmtUptime(status.uptimeSeconds)}</span></Row>}
          {memText && <Row label="Memory"><span>{memText}</span></Row>}
          {cache && <Row label="Relation cache"><span>{cache.entries} cached · {fmtBytes(cache.bytes)}</span></Row>}
          {(status?.inflight ?? 0) > 0 && <Row label="In flight"><span>{status?.inflight} preview(s)</span></Row>}
          {(status?.activeRuns ?? 0) > 0 && <Row label="Active runs"><span>{status?.activeRuns}</span></Row>}
          {cat === 'offline' && <p className="text-muted-foreground">Kernel unreachable — showing last-known state.</p>}
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
      </PopoverContent>
    </Popover>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right text-foreground">{children}</span>
    </div>
  )
}
