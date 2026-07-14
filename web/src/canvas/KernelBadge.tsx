import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
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
  if (status.reachable === false || status.stale) return 'stale'  // a live lease we can't reach is NOT warm
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
  // a run starting/finishing warms/changes the kernel — refresh the always-visible badge on that edge
  const runActive = useStore((s) => Object.values(s.runs).some((r) => r.phase === 'running' || r.phase === 'estimating'))
  const canEdit = roleCanEdit(canvasRole)
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<CanvasKernelStatus | null>(null)
  const [offline, setOffline] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const seq = useRef(0)  // discards a late response from a previous canvas / superseded poll

  const refresh = useCallback(async () => {
    if (!canvasId) return
    const s = ++seq.current
    try {
      const next = await api.kernelState(canvasId)
      if (s === seq.current) { setStatus(next); setOffline(false) }
    } catch {
      if (s === seq.current) setOffline(true)  // transient — keep last-known, don't fabricate 'cold'
    }
  }, [canvasId])

  // switching canvases must never show the previous canvas's kernel: reset + invalidate any in-flight
  useEffect(() => { setStatus(null); setOffline(false); seq.current++ }, [canvasId])

  // Refresh on canvas change and on run lifecycle (event-driven, so the always-visible badge stays
  // truthful without a steady-state timer); poll every 3s only while the popover is OPEN. The kernel
  // endpoint returns {exists:false} for a not-yet-persisted canvas, so this never logs a 404.
  useEffect(() => {
    void refresh()
    if (!open) return
    const id = window.setInterval(() => void refresh(), 3_000)
    return () => window.clearInterval(id)
  }, [refresh, runActive, open])

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
      : cat === 'stale' ? (status?.reachable === false ? 'unreachable' : 'stale')
        : (status?.state ?? 'ready')

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
