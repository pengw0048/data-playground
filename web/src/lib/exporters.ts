// Real exports — download the node's data or the whole canvas. No stubs.
import { api } from '../api/client'
import { previewPlanIdentity, useStore } from '../store/graph'
import { nodeOutputs } from '../nodes/registry'

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

// Export one explicitly-scoped preview sample as JSON + CSV. This never reuses the panel's current
// page: a page and a fresh bounded sample are different user intents and receive different filenames.
export async function exportNode(id: string) {
  const initial = useStore.getState()
  const { doc } = initial
  const node = doc.nodes.find((n) => n.id === id)
  if (!node) return
  const ports = nodeOutputs(node)
  const cached = initial.previews[id]
  const selectedPortId = ports.length > 1
    ? ports.find((port) => port.id === cached?.portId)?.id
    : undefined
  if (ports.length > 1 && !selectedPortId) {
    initial.pushToast('Choose an output in Data before exporting this multi-output node.', 'info')
    return
  }
  const baseName = (node.data.title || node.type).replace(/[^a-z0-9_-]+/gi, '_')
  const portName = selectedPortId?.replace(/[^a-z0-9_-]+/gi, '_')
  const name = portName ? `${baseName}-${portName}` : baseName
  const requestKey = `${doc.id}\u0000${id}`
  const generation = ++nextExportRequestGeneration
  exportRequestGeneration.set(requestKey, generation)
  const planIdentity = previewPlanIdentity(doc, id, selectedPortId)
  let res
  try {
    res = await api.preview(doc, id, 500, 0, selectedPortId)
  } catch (error) {
    if (exportRequestGeneration.get(requestKey) === generation) {
      exportRequestGeneration.delete(requestKey)
      const current = useStore.getState()
      if (!current.doc.nodes.some((candidate) => candidate.id === id)
          || previewPlanIdentity(current.doc, id, selectedPortId) !== planIdentity) {
        current.pushToast('Export cancelled — the graph changed while the preview sample was loading. Try again.', 'info')
      } else {
        current.pushToast(
          `Preview sample export failed${error instanceof Error && error.message ? `: ${error.message}` : ''}`,
          'error',
        )
      }
    }
    return
  }
  // A direct export refresh deliberately stays outside the panel's 50-row pagination state. Fence it
  // to both the latest export intent for this node and the exact graph snapshot sent to the kernel.
  // A response for an edited graph is never downloaded as if it belonged to the current document.
  const current = useStore.getState()
  if (exportRequestGeneration.get(requestKey) !== generation) return
  exportRequestGeneration.delete(requestKey)
  if (!current.doc.nodes.some((candidate) => candidate.id === id)
      || previewPlanIdentity(current.doc, id, selectedPortId) !== planIdentity) {
    current.pushToast('Export cancelled — the graph changed while the sample was loading. Try again.', 'info')
    return
  }
  if (res.notPreviewable) {
    current.pushToast('This node cannot produce a preview sample. Run it, then export the committed full result from Data or Run history.', 'info')
    return
  }
  if (res.error) {
    current.pushToast(`Preview sample export failed${res.reason ? `: ${res.reason}` : ''}`, 'error')
    return
  }
  const cols = res.columns.map((c) => c.name)
  download(`${name}-preview-sample.json`, JSON.stringify(res.rows, null, 2))
  download(`${name}-preview-sample.csv`, toCsv(cols, res.rows), 'text/csv')
  if (res.sampleProvenance) {
    download(`${name}-preview-sample.provenance.json`, JSON.stringify({
      sampleProvenance: res.sampleProvenance,
    }, null, 2))
  }
  useStore.getState().pushToast(
    `Exported preview sample — ${res.rows.length} rows as JSON + CSV. This is not the full result.`,
    'info',
  )
}

// Export through the server so the file has a versioned native envelope and cannot leak local run
// history or credential-bearing configuration from a raw client snapshot.
export async function exportCanvas() {
  const current = useStore.getState()
  const currentDraft = current.localDrafts.find((draft) => draft.draftId === current.currentDraftId)
  if (!current.kernelUp) {
    current.pushToast('Native Canvas export is unavailable offline. Reconnect and wait for the Canvas to finish saving.', 'info')
    return
  }
  if (current.currentDraftId) {
    current.pushToast(
      currentDraft?.syncState === 'syncing'
        ? 'Wait for the local Canvas draft to finish syncing before exporting.'
        : 'Resolve or retry the local Canvas draft and wait for it to sync before exporting.',
      'info',
    )
    return
  }
  if (!current.saved) {
    current.pushToast('Wait for the Canvas to finish saving before exporting.', 'info')
    return
  }
  try {
    const envelope = await api.nativeCanvasExport(current.doc.id)
    const base = (current.doc.name || current.doc.id).replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[.-]+|[.-]+$/g, '') || 'canvas'
    // Keep the downloaded representation within the same 2 MiB bound the server validated. Pretty
    // whitespace could otherwise make a canonical, valid envelope impossible to import again.
    download(`${base}.dp-canvas.json`, JSON.stringify(envelope))
    current.pushToast('Exported native Canvas document.', 'success')
  } catch (error) {
    current.pushToast(error instanceof Error ? error.message : 'Native Canvas export failed.', 'error')
  }
}
