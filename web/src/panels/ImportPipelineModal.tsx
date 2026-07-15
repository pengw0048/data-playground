import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { useStore } from '../store/graph'
import { Icon } from '../ui/Icon'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'

// Paste a foreign pipeline definition; the registered importer plugin (reg.set_importer) parses it into
// a runnable canvas graph, which we drop onto a FRESH file (applyAgentGraph REPLACES the canvas, so we
// never clobber the open one). The generic core ships no importer → import 501s → surfaced as a toast.
const PLACEHOLDER = `{
  "source": "my_table_or_uri",
  "steps": [
    {"filter": "amount > 0"},
    {"select": "id, amount"}
  ],
  "write": {"name": "out"}
}`

export function ImportPipelineModal({ onClose }: { onClose: () => void }) {
  const newFile = useStore((s) => s.newFile)
  const applyAgentGraph = useStore((s) => s.applyAgentGraph)
  const pushToast = useStore((s) => s.pushToast)
  const [config, setConfig] = useState('')
  const [busy, setBusy] = useState(false)
  const mountedRef = useRef(false)
  const generationRef = useRef(0)
  const activeRef = useRef<{ generation: number; controller: AbortController } | null>(null)

  const invalidate = useCallback(() => {
    generationRef.current += 1
    activeRef.current?.controller.abort()
    activeRef.current = null
  }, [])

  const close = useCallback(() => {
    invalidate()
    if (mountedRef.current) setBusy(false)
    onClose()
  }, [invalidate, onClose])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      invalidate()
    }
  }, [invalidate])

  const run = async () => {
    const text = config.trim()
    if (!text) return
    // Restarting with edited input supersedes every stage of the previous generation.
    activeRef.current?.controller.abort()
    const active = { generation: ++generationRef.current, controller: new AbortController() }
    activeRef.current = active
    const isCurrent = () => mountedRef.current
      && activeRef.current === active
      && generationRef.current === active.generation
      && !active.controller.signal.aborted
    setBusy(true)
    try {
      const res = await api.importPipeline(text, undefined, { signal: active.controller.signal })
      if (!isCurrent()) return
      if (res.graph && res.graph.nodes.length) {
        const created = await newFile({ signal: active.controller.signal })
        if (!isCurrent() || !created.ok) return
        // applyAgentGraph replaces nodes/edges, so bind it to the canvas just created. A completed
        // creation must not let a late import replace a canvas the researcher navigated to instead.
        if (!applyAgentGraph(res.graph, created.canvasId)) return
        if (!isCurrent()) return
        pushToast(`Imported ${res.graph.nodes.length} nodes`, 'success')
        activeRef.current = null
        setBusy(false)
        onClose()
      } else {
        pushToast('The importer described the pipeline but returned no graph to run', 'info')
      }
    } catch (e) {
      if (!isCurrent()) return
      // no importer registered ⇒ the endpoint replies 501; surface whatever it said plainly
      pushToast((e as Error).message || 'Import failed', 'error')
    } finally {
      if (isCurrent()) {
        activeRef.current = null
        setBusy(false)
      }
    }
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) close() }}>
      <DialogContent className="dp-modal-overlay gap-0 overflow-hidden p-0 w-[520px] max-w-[92vw] rounded-xl">
        <div className="flex items-center gap-2 border-b border-border py-3 pl-4 pr-12">
          <span className="flex items-center text-muted-foreground"><Icon name="import" size={14} /></span>
          <DialogTitle className="text-sm font-semibold">Import pipeline</DialogTitle>
        </div>
        <DialogDescription className="sr-only">Paste a pipeline definition to import it as a runnable canvas.</DialogDescription>

        <div className="flex flex-col gap-3 p-4">
          <div className="text-[11.5px] text-muted-foreground">
            Paste a pipeline definition. It's parsed by the registered importer plugin and dropped onto a new canvas.
          </div>
          <textarea
            value={config}
            onChange={(e) => setConfig(e.target.value)}
            placeholder={PLACEHOLDER}
            spellCheck={false}
            rows={12}
            className="dp-mono w-full resize-y rounded-md border border-border bg-background px-2.5 py-2 text-[11.5px] text-foreground outline-none"
          />
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={close}>Cancel</Button>
            <Button size="sm" disabled={!config.trim()} onClick={run}>
              <Icon name="import" size={13} /> {busy ? 'Restart import' : 'Import'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
