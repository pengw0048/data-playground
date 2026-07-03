// Real exports — download the node's data or the whole canvas. No stubs.
import { api } from '../api/client'
import { useStore } from '../store/graph'

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
  const { doc } = useStore.getState()
  const node = doc.nodes.find((n) => n.id === id)
  if (!node) return
  const name = (node.data.title || node.type).replace(/[^a-z0-9_-]+/gi, '_')
  let res = useStore.getState().previews[id]?.result
  if (!res) {
    try {
      res = await api.preview(doc, id, 500)
    } catch {
      return
    }
  }
  if (!res || res.notPreviewable) {
    download(`${name}.json`, JSON.stringify({ note: 'not sample-previewable — run a full pass', node: node.data }, null, 2))
    return
  }
  const cols = res.columns.map((c) => c.name)
  download(`${name}.json`, JSON.stringify(res.rows, null, 2))
  download(`${name}.csv`, toCsv(cols, res.rows), 'text/csv')
}

// Export the whole canvas as a portable JSON document (NFR-7).
export function exportCanvas() {
  const { doc } = useStore.getState()
  download(`${doc.name || doc.id}.canvas.json`, JSON.stringify(doc, null, 2))
}
