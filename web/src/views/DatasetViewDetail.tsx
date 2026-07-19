import { useEffect, useRef, useState } from 'react'
import { api, KernelError } from '../api/client'
import type { DatasetViewDefinition, DatasetViewPreview } from '../types/api'
import { Icon } from '../ui/Icon'
import { DistributionReportLauncher } from './DistributionReports'

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)
const cell = (value: unknown) => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value)

export function DatasetViewDetail({ definition, onClose, onDeleted }: {
  definition: DatasetViewDefinition
  onClose: () => void
  onDeleted: () => void
}) {
  const [preview, setPreview] = useState<DatasetViewPreview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const previewGeneration = useRef(0)
  const deleteGeneration = useRef(0)

  const load = async () => {
    const request = ++previewGeneration.current
    setLoading(true); setError(null)
    try {
      const next = await api.previewDatasetView(definition.id)
      if (request === previewGeneration.current) setPreview(next)
    } catch (caught) {
      if (request !== previewGeneration.current) return
      const exactUnavailable = caught instanceof KernelError && caught.status === 410
      setError(exactUnavailable
        ? 'This exact revision is no longer available. The view did not substitute the current head.'
        : errorMessage(caught))
    } finally {
      if (request === previewGeneration.current) setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    return () => { previewGeneration.current += 1; deleteGeneration.current += 1 }
  }, [definition.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const remove = async () => {
    if (deleting) return
    const request = ++deleteGeneration.current
    setDeleting(true); setDeleteError(null)
    try {
      await api.deleteDatasetView(definition.id)
      if (request === deleteGeneration.current) {
        previewGeneration.current += 1
        onDeleted()
      }
    } catch (caught) {
      if (request === deleteGeneration.current) setDeleteError(errorMessage(caught))
    } finally {
      if (request === deleteGeneration.current) setDeleting(false)
    }
  }

  const evidence = definition.sampleProvenance
  const temporalWindow = definition.temporalWindow
  const requestClose = () => { if (!deleting) onClose() }
  return <div className="fixed inset-0 z-40 flex justify-end bg-black/20" onClick={requestClose}>
    <div role="dialog" aria-modal="true" aria-label={definition.name} aria-busy={deleting} onClick={(event) => event.stopPropagation()}
      className="flex h-full w-[560px] max-w-full flex-col border-l border-border bg-card shadow-xl">
      <header className="flex items-center gap-2 border-b border-border px-5 py-4">
        <Icon name="sample" size={16} />
        <div className="min-w-0 flex-1"><h2 className="truncate text-[14px] font-bold">{definition.name}</h2>
          <div className="text-[10px] text-muted-foreground">Immutable DatasetView · schema v{definition.schemaVersion}</div></div>
        <button onClick={requestClose} disabled={deleting} aria-label="Close DatasetView detail"><Icon name="close" size={15} /></button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        <div className="grid gap-4 text-[11px]">
          <section className="grid gap-1 rounded-lg border border-border bg-muted/20 p-3">
            <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Exact source</div>
            <div className="break-all font-mono">dataset:{definition.datasetRef.datasetId}</div>
            <div className="break-all font-mono">revision:{definition.datasetRef.revisionId}</div>
            <div className="text-muted-foreground">Committed {definition.datasetRef.lastKnown?.committedAt
              ? new Date(definition.datasetRef.lastKnown.committedAt).toLocaleString() : 'time not provided'} · retained by {definition.retentionOwner}</div>
          </section>
          <section className="grid gap-1.5">
            <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Definition</div>
            <div className="flex flex-wrap gap-1">{definition.selectedColumns.map((column) => <span key={column} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">{column}</span>)}</div>
            <div className="rounded-md border border-border bg-muted/25 p-2 font-mono text-[10.5px]">{definition.predicate || 'No additional predicate'}</div>
            {temporalWindow && <div className="rounded-md border border-border bg-muted/25 p-2">
              <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Temporal window</div>
              <div className="mt-1 font-mono text-[10.5px]">{temporalWindow.timeField} [{temporalWindow.startTick}, {temporalWindow.endTick})</div>
              <div className="text-muted-foreground">Time domain: {temporalWindow.timeDomain}</div>
            </div>}
            <div>{definition.sampling.kind === 'all'
              ? 'All matching rows'
              : `Deterministic reservoir · ${definition.sampling.size.toLocaleString()} rows · seed ${definition.sampling.seed}`}</div>
            {evidence && <div className="rounded-md border border-border p-2 text-muted-foreground">
              <div><strong className="text-foreground">Sampling evidence:</strong> {evidence.returnedRows.toLocaleString()} rows returned{evidence.totalRows != null ? ` from ${evidence.totalRows.toLocaleString()}` : ''}</div>
              {evidence.limitations.map((limitation) => <div key={limitation} className="mt-1">{limitation}</div>)}
              <div className="mt-1 break-all font-mono text-[9px]">identity:{evidence.identity}</div>
            </div>}
          </section>
          <section className="grid gap-2">
            <div className="flex items-center gap-2"><div className="flex-1 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Bounded preview</div>
              {!loading && <button onClick={() => void load()} className="font-semibold text-primary underline">Refresh exact preview</button>}</div>
            {loading ? <div role="status" className="rounded-md border border-border p-3 text-muted-foreground">Replaying the exact definition…</div>
              : error ? <div role="alert" className="rounded-md border border-destructive/30 p-3 text-destructive"><div>{error}</div><button onClick={() => void load()} className="mt-2 font-semibold underline">Retry</button></div>
                : preview ? <PreviewTable preview={preview} /> : null}
          </section>
          <DistributionReportLauncher definition={definition} />
          <section className="grid gap-1 text-[9.5px] text-muted-foreground">
            <div>Created {new Date(definition.createdAt).toLocaleString()} by {definition.creatorId}</div>
            <div className="break-all font-mono">semantic:{definition.semanticSha256}</div>
            <div className="break-all font-mono">definition:{definition.definitionSha256}</div>
          </section>
        </div>
      </div>
      <footer className="border-t border-border p-4">
        {deleteError && <div role="alert" className="mb-2 text-[11px] text-destructive">Couldn't delete this view: {deleteError}</div>}
        {confirmDelete ? <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-2 text-[11px]">
          <span className="min-w-0 flex-1">Delete this view and release its revision hold? The submission remains tombstoned.</span>
          <button onClick={() => setConfirmDelete(false)} disabled={deleting} className="font-semibold underline">Cancel</button>
          <button onClick={() => void remove()} disabled={deleting} className="rounded bg-destructive px-2 py-1 font-semibold text-destructive-foreground disabled:opacity-50">{deleting ? 'Deleting…' : 'Delete'}</button>
        </div> : <button onClick={() => setConfirmDelete(true)} className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-destructive"><Icon name="trash" size={13} /> Delete view</button>}
      </footer>
    </div>
  </div>
}

function PreviewTable({ preview }: { preview: DatasetViewPreview }) {
  if (!preview.rows.length) return <div className="rounded-md border border-border p-3 text-muted-foreground">This exact definition returned no rows.</div>
  return <div>
    <div className="mb-1 text-[10px] text-muted-foreground">Showing {preview.rows.length.toLocaleString()}{preview.rowCount != null ? ` of ${preview.rowCount.toLocaleString()}` : ''} rows{preview.hasMore ? ` · truncated at ${preview.rowLimit}` : ''}</div>
    <div className="max-h-[300px] overflow-auto rounded-md border border-border">
      <table className="w-max font-mono text-[9.5px]"><thead><tr>{preview.columns.map((column) => <th key={column.name} className="sticky top-0 border-b border-border bg-muted px-2 py-1 text-left">{column.name}</th>)}</tr></thead>
        <tbody>{preview.rows.map((row, index) => <tr key={index}>{preview.columns.map((column) => <td key={column.name} className="max-w-[220px] truncate whitespace-nowrap border-b border-border/40 px-2 py-0.5">{cell(row[column.name])}</td>)}</tr>)}</tbody></table>
    </div>
  </div>
}
