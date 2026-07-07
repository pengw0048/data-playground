import { useState } from 'react'
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

  const run = async () => {
    const text = config.trim()
    if (!text || busy) return
    setBusy(true)
    try {
      const res = await api.importPipeline(text)
      if (res.graph && res.graph.nodes.length) {
        await newFile()  // import into a fresh canvas — applyAgentGraph replaces nodes/edges
        applyAgentGraph(res.graph)
        pushToast(`Imported ${res.graph.nodes.length} nodes`, 'success')
        onClose()
      } else {
        pushToast('The importer described the pipeline but returned no graph to run', 'info')
      }
    } catch (e) {
      // no importer registered ⇒ the endpoint replies 501; surface whatever it said plainly
      pushToast((e as Error).message || 'Import failed', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
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
            <Button variant="outline" size="sm" onClick={onClose}>Cancel</Button>
            <Button size="sm" disabled={!config.trim() || busy} onClick={run}>
              <Icon name="import" size={13} /> {busy ? 'Importing…' : 'Import'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
