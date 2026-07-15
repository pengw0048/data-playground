// Real exports — download the node's data or the whole canvas. No stubs.
import { api } from '../api/client'
import { previewIsCurrent, previewPlanIdentity, useStore } from '../store/graph'

const exportRequestGeneration = new Map<string, number>()
let nextExportRequestGeneration = 0

function download(filename: string, text: string, mime = 'application/json') {
  const blob = new Blob([text], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function toCsv(columns: string[], rows: Record<string, unknown>[]): string {
  const esc = (v: unknown) => {
    const s = v == null ? '' : Array.isArray(v) ? JSON.stringify(v) : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  return [columns.join(','), ...rows.map((r) => columns.map((c) => esc(r[c])).join(','))].join('\n')
}

// Export a node's current output (runs a preview to fetch rows) as JSON + CSV.
export async function exportNode(id: string) {
  const initial = useStore.getState()
  const { doc } = initial
  const node = doc.nodes.find((n) => n.id === id)
  if (!node) return
  const name = (node.data.title || node.type).replace(/[^a-z0-9_-]+/gi, '_')
  const requestKey = `${doc.id}\u0000${id}`
  const generation = ++nextExportRequestGeneration
  exportRequestGeneration.set(requestKey, generation)
  const planIdentity = previewPlanIdentity(doc, id)
  const cached = initial.previews[id]
  let res = cached && previewIsCurrent(cached, doc, id) ? cached.result : undefined
  if (!res) {
    try {
      res = await api.preview(doc, id, 500)
    } catch (error) {
      if (exportRequestGeneration.get(requestKey) === generation) {
        exportRequestGeneration.delete(requestKey)
        const current = useStore.getState()
        if (!current.doc.nodes.some((candidate) => candidate.id === id)
            || previewPlanIdentity(current.doc, id) !== planIdentity) {
          current.pushToast('Export cancelled — the graph changed while the sample was loading. Try again.', 'info')
        } else {
          current.pushToast(
            `Export failed${error instanceof Error && error.message ? `: ${error.message}` : ''}`,
            'error',
          )
        }
      }
      return
    }
  }
  // A direct export refresh deliberately stays outside the panel's 50-row pagination state. Fence it
  // to both the latest export intent for this node and the exact graph snapshot sent to the kernel.
  // A response for an edited graph is never downloaded as if it belonged to the current document.
  const current = useStore.getState()
  if (exportRequestGeneration.get(requestKey) !== generation) return
  exportRequestGeneration.delete(requestKey)
  if (!current.doc.nodes.some((candidate) => candidate.id === id)
      || previewPlanIdentity(current.doc, id) !== planIdentity) {
    current.pushToast('Export cancelled — the graph changed while the sample was loading. Try again.', 'info')
    return
  }
  if (!res || res.notPreviewable) {
    download(`${name}.json`, JSON.stringify({ note: 'not sample-previewable — run a full pass', node: node.data }, null, 2))
    return
  }
  const cols = res.columns.map((c) => c.name)
  download(`${name}.json`, JSON.stringify(res.rows, null, 2))
  download(`${name}.csv`, toCsv(cols, res.rows), 'text/csv')
  // be honest that this is a sampled export, not the full dataset — and that two files downloaded
  useStore.getState().pushToast(`Exported ${name} — sampled ${res.rows.length} rows (JSON + CSV). For the full dataset, add a write node.`, 'info')
}

// Export the whole canvas as a portable JSON document (NFR-7).
export function exportCanvas() {
  const { doc } = useStore.getState()
  download(`${doc.name || doc.id}.canvas.json`, JSON.stringify(doc, null, 2))
}
