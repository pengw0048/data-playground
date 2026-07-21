import { useState } from 'react'
import { api } from '../api/client'
import type { DatasetRevisionDetail, RunOutput, WriteAdmission, WriteReceipt } from '../types/api'

export function publicationMode(mode: WriteAdmission['mode'] | undefined): string {
  if (mode === 'create') return 'Create a new dataset'
  if (mode === 'append') return 'Append to the selected dataset'
  if (mode === 'replace' || mode === 'overwrite') return 'Replace the selected dataset'
  return 'Publication mode is not available yet'
}

function ExactRevisionAction({ receipt }: { receipt: WriteReceipt }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [detail, setDetail] = useState<DatasetRevisionDetail | null>(null)
  const open = async () => {
    setLoading(true); setError(''); setDetail(null)
    try {
      // A receipt supplies both immutable ids. Never resolve or substitute a latest revision here.
      setDetail(await api.datasetRevision(receipt.datasetId, receipt.revisionId))
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
    finally { setLoading(false) }
  }
  const schemaFieldCount = detail?.preview?.columns?.length ?? 0
  return <>
    <button type="button" className="ml-2 font-semibold text-primary underline" onClick={() => void open()} disabled={loading}>
      {loading ? 'Opening exact revision…' : 'Open exact revision'}
    </button>
    {detail && <div aria-label="Exact revision detail" className="mt-2 rounded border border-border bg-background p-2 text-muted-foreground">
      <div className="font-semibold text-foreground">Exact revision {detail.datasetId}@{detail.revisionId}</div>
      <div>Committed {detail.committedAt ?? 'unknown'}</div>
      <div>{detail.summary?.rowCount?.toLocaleString?.() ?? 'unknown'} rows · {schemaFieldCount} schema {schemaFieldCount === 1 ? 'field' : 'fields'}</div>
      <div>{detail.parentRevisionId ? <>Parent <span className="font-mono">{detail.parentRevisionId}</span></> : 'No parent revision'}</div>
    </div>}
    {error && <div role="alert" className="mt-1 text-destructive">Exact revision unavailable: {error}. Latest was not substituted.</div>}
  </>
}

function schemaText(fields: { name: string; type: string }[]): string {
  return fields.length ? fields.map((field) => `${field.name}: ${field.type}`).join(', ') : 'unknown'
}

function partitionText(partitions: { field: string }[]): string {
  return partitions.length ? partitions.map((partition) => partition.field).join(', ') : 'unpartitioned'
}

function AdmissionDetails({ label, admission }: { label: string; admission: WriteAdmission }) {
  return <>
    <div><strong>{label}:</strong> node <span className="font-mono">{admission.nodeId}</span> · {admission.managed ? 'managed' : 'provider-neutral'} · mode <span className="font-mono">{admission.mode}</span></div>
    <div><strong>Provider:</strong> <span className="font-mono">{admission.provider}</span></div>
    <div><strong>Admission destination:</strong> <span className="font-mono">{admission.destination}</span></div>
    <div><strong>Schema:</strong> {schemaText(admission.expectedSchema)}</div>
    <div><strong>Partitions:</strong> {partitionText(admission.partitions)}</div>
    {admission.expectedHead && <div><strong>Expected head:</strong> <span className="font-mono">{admission.expectedHead.datasetId}@{admission.expectedHead.revisionId}</span></div>}
    {admission.intent && <>
      <div><strong>Frozen destination:</strong> <span className="font-mono">{admission.intent.destination.logicalUri}</span> · {admission.intent.destination.name} · {admission.intent.destination.provider}{admission.intent.destination.datasetId ? ` · dataset ${admission.intent.destination.datasetId}` : ''}</div>
      <div><strong>Idempotency key:</strong> <span className="font-mono">{admission.intent.idempotencyKey}</span></div>
      <div><strong>Frozen provenance:</strong> <span className="font-mono">{JSON.stringify(admission.intent.provenance)}</span></div>
    </>}
  </>
}

function sameAdmission(left: WriteAdmission | null | undefined, right: WriteAdmission | null | undefined): boolean {
  if (!left || !right) return false
  return left.mode === right.mode && left.destination === right.destination
    && left.intent?.idempotencyKey === right.intent?.idempotencyKey
}

function PublicationDetails({ admission, outcomeAdmission, receipt, outputs = [] }: {
  admission?: WriteAdmission | null; outcomeAdmission?: WriteAdmission | null; receipt?: WriteReceipt | null; outputs?: RunOutput[]
}) {
  if (!admission && !outcomeAdmission && !receipt && outputs.length === 0) return null
  return <details className="mt-2 rounded-md border border-border bg-muted/20 px-2 py-1.5 text-[10.5px] text-muted-foreground">
    <summary className="cursor-pointer font-semibold text-foreground">Publication details</summary>
    <div className="mt-2 grid gap-1 break-all">
      {outcomeAdmission && <AdmissionDetails label="Completed admission" admission={outcomeAdmission} />}
      {admission && !sameAdmission(admission, outcomeAdmission)
        && <AdmissionDetails label={outcomeAdmission ? 'Next admission' : 'Admission'} admission={admission} />}
      {receipt && <>
        <div><strong>Receipt:</strong> <span className="font-mono">{receipt.datasetId}@{receipt.revisionId}</span></div>
        <div><strong>Durable:</strong> yes</div>
        <div><strong>Head:</strong> <span className="font-mono">{receipt.head.datasetId}@{receipt.head.revisionId}</span>{receipt.head.committedAt ? ` · committed ${receipt.head.committedAt}` : ''} · retention {receipt.head.retentionOwner}</div>
        <div><strong>Rows:</strong> {receipt.rows.toLocaleString()}</div>
        <div><strong>Bytes:</strong> {receipt.bytes.toLocaleString()}</div>
        <div><strong>Receipt schema:</strong> {schemaText(receipt.schema)}</div>
        <div><strong>Receipt partitions:</strong> {partitionText(receipt.partitions)}</div>
        <div><strong>Publication provider:</strong> <span className="font-mono">{receipt.publication.provider}</span></div>
        <div><strong>Logical URI:</strong> <span className="font-mono">{receipt.publication.logicalUri}</span></div>
        <div><strong>Artifact URI:</strong> <span className="font-mono">{receipt.publication.artifactUri}</span></div>
        <div><strong>Publication sequence:</strong> {receipt.publication?.publishSequence ?? 'unknown'}</div>
        <div><strong>Idempotency key:</strong> <span className="font-mono">{receipt.publication?.idempotencyKey ?? 'unknown'}</span></div>
        <div><strong>Catalog version:</strong> {receipt.publication?.catalogVersion ?? 'unknown'}</div>
        {receipt.parentHead && <div><strong>Parent:</strong> <span className="font-mono">{receipt.parentHead.datasetId}@{receipt.parentHead.revisionId}</span></div>}
        <div><strong>Backend:</strong> {receipt.publication?.backendVersion ?? 'unknown'}</div>
        {receipt.executionManifestSha256 && <div><strong>Execution manifest:</strong> <span className="font-mono">{receipt.executionManifestSha256}</span></div>}
      </>}
      {outputs.map((output) => <div key={`${output.nodeId}:${output.portId}`} className="mt-1 rounded border border-border bg-background p-1.5" aria-label="Write output evidence">
        <div><strong>Output:</strong> <span className="font-mono">{output.nodeId}:{output.portId}</span>{output.portLabel ? ` · ${output.portLabel}` : ''}</div>
        <div><strong>Outcome:</strong> {output.outcome} · {output.publicationKind} · {output.wire}</div>
        {output.uri && <div><strong>URI:</strong> <span className="font-mono">{output.uri}</span></div>}
        {output.table && <div><strong>Table:</strong> <span className="font-mono">{output.table}</span></div>}
        {output.version && <div><strong>Version:</strong> <span className="font-mono">{output.version}</span></div>}
        {output.rows != null && <div><strong>Output rows:</strong> {output.rows.toLocaleString()}</div>}
        {output.error && <div className="text-destructive"><strong>Error:</strong> {output.error}</div>}
      </div>)}
    </div>
  </details>
}

export function WritePublicationSummary({ outputName, destination, admission, outcomeAdmission, receipt, outputs, compact = false, completed = false }: {
  outputName: string; destination: string; admission?: WriteAdmission | null; outcomeAdmission?: WriteAdmission | null; receipt?: WriteReceipt | null; outputs?: RunOutput[]; compact?: boolean; completed?: boolean
}) {
  const classes = compact ? 'mt-2 text-[10.5px]' : 'rounded-md border border-border bg-muted/30 p-2 text-[11px]'
  return <section aria-label="Write publication" className={classes}>
    <div className="grid gap-1.5">
      <div><span className="font-semibold text-foreground">Output name</span><div className="font-mono text-foreground">{outputName}</div></div>
      <div><span className="font-semibold text-foreground">Destination</span><div className="text-muted-foreground">{destination}</div></div>
      <div><span className="font-semibold text-foreground">Publication mode</span><div className="text-muted-foreground">{publicationMode(admission?.mode)}</div></div>
      {admission?.blocker ? <div aria-label="Write blocker" role="alert" className="rounded border border-destructive/30 bg-destructive/10 px-2 py-1 text-destructive">
        <strong>Cannot publish until</strong> {admission.blocker}
      </div> : receipt ? <div aria-label="Write readiness" className="text-emerald-700 dark:text-emerald-300">Exact publication receipt recorded.</div>
        : completed ? <div aria-label="Write readiness" role="status" className="text-muted-foreground">Publication outcome is unknown; no exact receipt was recorded.</div>
        : admission ? <div aria-label="Write readiness" className="text-emerald-700 dark:text-emerald-300">Ready to publish</div>
        : <div aria-label="Write readiness" className="text-muted-foreground">Readiness has not been checked yet.</div>}
      {receipt && <div aria-label="Published result" className="rounded border border-emerald-500/30 bg-emerald-500/5 px-2 py-1.5 text-foreground">
        <strong>{outputName} published</strong> · {receipt.rows.toLocaleString()} rows
        <ExactRevisionAction key={`${receipt.datasetId}:${receipt.revisionId}`} receipt={receipt} />
      </div>}
    </div>
    <PublicationDetails admission={admission} outcomeAdmission={outcomeAdmission} receipt={receipt} outputs={outputs} />
  </section>
}
