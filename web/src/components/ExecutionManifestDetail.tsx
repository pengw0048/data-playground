import { useEffect, useState } from 'react'
import {
  api,
  type ExecutionManifestAvailability,
  type ExecutionManifestDetail as ExecutionManifestDetailDto,
} from '../api/client'
import { Badge } from '@/components/ui/badge'

interface ManifestSummary {
  executionManifestSha256?: string | null
  executionManifestSchemaVersion?: number | null
  executionManifestAvailability?: ExecutionManifestAvailability
}

const availabilityLabel: Record<ExecutionManifestAvailability, string> = {
  available: 'available',
  pruned: 'pruned',
  not_recorded: 'not recorded',
  unavailable: 'unavailable',
  corrupt: 'corrupt',
}

const availabilityMessage: Record<Exclude<ExecutionManifestAvailability, 'available'>, string> = {
  pruned: 'The retained manifest document was pruned. Live Canvas state was not substituted.',
  not_recorded: 'This legacy or summary-only item did not record an execution manifest.',
  unavailable: 'The manifest subject or its schema is unavailable. Live Canvas and current plugin state were not substituted.',
  corrupt: 'The retained manifest failed integrity or secret-safety validation and was not returned.',
}

export function ExecutionManifestDetail({
  canvasId,
  subjectId,
  summary,
  onClone,
}: {
  canvasId: string
  subjectId: string
  summary: ManifestSummary
  onClone?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<ExecutionManifestDetailDto | null>(null)
  const [error, setError] = useState('')
  const [generation, setGeneration] = useState(0)
  const availability = summary.executionManifestAvailability ?? (
    summary.executionManifestSha256 ? 'available' : 'not_recorded'
  )

  useEffect(() => {
    if (!open) return
    let live = true
    setDetail(null)
    setError('')
    void api.executionManifest(canvasId, subjectId).then((value) => {
      if (live) setDetail(value)
    }).catch((caught: unknown) => {
      if (!live) return
      const status = typeof caught === 'object' && caught !== null
        ? (caught as { status?: unknown }).status : undefined
      if (status === 404) {
        setDetail({ availability: 'unavailable', document: null })
      } else {
        setError(caught instanceof Error ? caught.message : String(caught))
      }
    })
    return () => { live = false }
  }, [canvasId, generation, open, subjectId])

  const activeAvailability = detail?.availability ?? availability
  const digest = detail?.sha256 ?? summary.executionManifestSha256
  const schemaVersion = detail?.schemaVersion ?? summary.executionManifestSchemaVersion

  return <section aria-label={`Execution manifest for ${subjectId}`} className="border-t border-border bg-muted/20">
    <button type="button" onClick={() => setOpen((value) => !value)} aria-expanded={open}
      className="flex w-full items-center gap-2 px-4 py-2 text-left text-[11px] hover:bg-muted/40">
      <span className="text-muted-foreground">{open ? '▾' : '▸'}</span>
      <span className="font-semibold text-foreground">Execution manifest</span>
      {schemaVersion != null && <Badge variant="outline" className="h-5 px-1.5 text-[9px]">v{schemaVersion}</Badge>}
      <Badge variant="secondary" className="h-5 px-1.5 text-[9px]">{availabilityLabel[activeAvailability]}</Badge>
      {digest && <span className="dp-mono min-w-0 truncate text-[9.5px] text-muted-foreground" title={digest}>{digest}</span>}
    </button>
    {open && <div className="grid gap-3 border-t border-border/60 px-4 py-3 text-[10.5px]">
      {!detail && !error && <div role="status" className="text-muted-foreground">Loading the retained manifest…</div>}
      {error && <div role="alert" className="text-destructive">
        Couldn’t inspect the retained manifest: {error}{' '}
        <button type="button" className="font-semibold underline" onClick={() => setGeneration((value) => value + 1)}>Retry</button>
      </div>}
      {detail && detail.availability !== 'available' && (
        <div className={detail.availability === 'corrupt' ? 'text-destructive' : 'text-muted-foreground'}>
          {availabilityMessage[detail.availability]}
        </div>
      )}
      {detail?.availability === 'available' && detail.document && <ManifestDocument detail={detail} onClone={onClone} />}
    </div>}
  </section>
}

function ManifestDocument({ detail, onClone }: { detail: ExecutionManifestDetailDto; onClone?: () => void }) {
  const document = detail.document!
  const target = document.target.nodeId
    ? `${document.target.nodeId}${document.target.portId ? `:${document.target.portId}` : ''}`
    : 'Whole graph'
  return <>
    {onClone && <button type="button" className="w-fit rounded-md border border-border bg-background px-2 py-1 font-semibold hover:bg-accent" onClick={onClone}>Clone as new Canvas…</button>}
    <div className="grid gap-1">
      <div><strong>Digest:</strong> <span className="dp-mono break-all">{detail.sha256}</span></div>
      <div><strong>Schema:</strong> version {detail.schemaVersion}</div>
      <div><strong>Target:</strong> <span className="dp-mono">{target}</span></div>
    </div>
    <ManifestSection title="Submitted graph" value={document.graph} />
    <section>
      <h4 className="mb-1 font-semibold text-foreground">Admitted inputs</h4>
      {document.admittedInputs.length === 0
        ? <div className="text-muted-foreground">No Source inputs were admitted.</div>
        : <ol className="grid gap-1">
          {document.admittedInputs.map((input, index) => <li key={`${input.nodeId}:${input.datasetId}:${input.revisionId}`} className="rounded border border-border bg-background p-2">
            <span className="font-semibold">{index + 1}. {input.nodeId}</span>
            <span className="dp-mono block break-all text-muted-foreground">{input.datasetId}@{input.revisionId} · {input.provider}</span>
          </li>)}
        </ol>}
    </section>
    <ManifestSection title="Admitted write intent" value={document.writeIntent} empty="No write intent was admitted." />
    <ManifestSection title="Runtime descriptor snapshot" value={document.descriptors} />
    <ManifestSection title="Declared parameter bindings" value={document.parameters} empty="No declared parameter bindings were recorded." />
  </>
}

function ManifestSection({ title, value, empty }: { title: string; value: unknown; empty?: string }) {
  return <section>
    <h4 className="mb-1 font-semibold text-foreground">{title}</h4>
    {value == null
      ? <div className="text-muted-foreground">{empty ?? 'Not recorded.'}</div>
      : <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-background p-2 text-[9.5px] text-foreground">{JSON.stringify(value, null, 2)}</pre>}
  </section>
}
