import { useState } from 'react'
import { api } from '../api/client'
import type {
  DatasetRevisionDetail, RunOutput, WriteAdmission, WriteReceipt, WriteSchemaDrift,
} from '../types/api'

export function publicationMode(mode: WriteAdmission['mode'] | undefined): string {
  if (mode === 'create') return 'Create a new dataset'
  if (mode === 'append') return 'Append to the selected dataset'
  if (mode === 'replace' || mode === 'overwrite') return 'Replace the selected dataset'
  return 'Revision mode is not available yet'
}

function writeMode(mode: WriteAdmission['mode'] | undefined): string {
  if (mode === 'append') return 'Append provider output'
  if (mode === 'replace' || mode === 'overwrite') return 'Overwrite provider output'
  if (mode === 'create') return 'Create provider output'
  return 'Write mode is not available yet'
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
    <button type="button"
      className="mt-2 inline-flex rounded-md bg-primary px-2.5 py-1.5 text-[10.5px] font-semibold text-primary-foreground shadow-sm disabled:opacity-60"
      onClick={() => void open()} disabled={loading}>
      {loading ? 'Loading published version…' : 'View published version'}
    </button>
    {detail && <div aria-label="Exact revision detail" className="mt-2 rounded border border-border bg-background p-2 text-muted-foreground">
      <div className="font-semibold text-foreground">Published dataset · version {detail.revisionId}</div>
      {detail.name && <div>Name <span className="font-mono">{detail.name}</span></div>}
      <div>Committed {detail.committedAt ?? 'unknown'}</div>
      <div>{detail.summary?.rowCount?.toLocaleString?.() ?? 'unknown'} rows · {schemaFieldCount} schema {schemaFieldCount === 1 ? 'field' : 'fields'}</div>
      <div>{detail.parentRevisionId ? <>Parent <span className="font-mono">{detail.parentRevisionId}</span></> : 'No parent revision'}</div>
    </div>}
    {error && <div role="alert" className="mt-1 text-destructive">Could not load this published version: {error}. A newer version was not substituted.</div>}
  </>
}

function schemaText(fields: { name: string; type: string }[]): string {
  return fields.length ? fields.map((field) => `${field.name}: ${field.type}`).join(', ') : 'unknown'
}

function partitionText(partitions: { field: string }[]): string {
  return partitions.length ? partitions.map((partition) => partition.field).join(', ') : 'unpartitioned'
}

function SchemaDriftEvidence({ evidence }: { evidence: WriteSchemaDrift }) {
  const visible = evidence.compatibility.fields.slice(0, 12)
  const hidden = evidence.compatibility.fields.length - visible.length
  return <div aria-label="Schema comparison" className="rounded border border-border bg-background px-2 py-1.5">
    <div className="font-semibold text-foreground">
      Exact schema comparison · {evidence.compatibility.status}
    </div>
    <div>
      Compared head <span className="font-mono">
        {evidence.comparedHead.datasetId}@{evidence.comparedHead.revisionId}
      </span>
    </div>
    <div className={evidence.requiresConfirmation ? 'font-semibold text-amber-700 dark:text-amber-300' : ''}>
      {evidence.requiresConfirmation
        ? 'Structural schema drift requires explicit confirmation.'
        : 'No structural schema drift requires confirmation.'}
    </div>
    {visible.map((field, index) => <div key={`${field.kind}:${field.fieldId ?? ''}:${field.oldName ?? ''}:${field.newName ?? ''}:${index}`}>
      {field.kind} · {field.status} · {field.oldName ?? '—'} → {field.newName ?? '—'} · {field.reason}
    </div>)}
    {hidden > 0 && <div>{hidden} more retained comparison fields are not shown.</div>}
  </div>
}

function AdmissionDetails({ label, admission }: { label: string; admission: WriteAdmission }) {
  const runtimeSchema = admission.intent?.schemaMode === 'runtime'
  return <>
    <div><strong>{label}:</strong> node <span className="font-mono">{admission.nodeId}</span> · {admission.managed ? 'managed' : 'provider-neutral'} · mode <span className="font-mono">{admission.mode}</span></div>
    <div><strong>Provider:</strong> <span className="font-mono">{admission.provider}</span></div>
    <div><strong>Admission destination:</strong> <span className="font-mono">{admission.destination}</span></div>
    <div><strong>Schema:</strong> {runtimeSchema
      ? 'Full output schema will be validated during this run.'
      : schemaText(admission.expectedSchema)}</div>
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

function PublicationDetails({ admission, outcomeAdmission, receipt, schemaDrift, outputs = [] }: {
  admission?: WriteAdmission | null; outcomeAdmission?: WriteAdmission | null; receipt?: WriteReceipt | null
  schemaDrift?: WriteSchemaDrift | null; outputs?: RunOutput[]
}) {
  if (!admission && !outcomeAdmission && !receipt && outputs.length === 0) return null
  return <details className="mt-2 rounded-md border border-border bg-muted/20 px-2 py-1.5 text-[10.5px] text-muted-foreground">
    <summary className="cursor-pointer font-semibold text-foreground">Technical details</summary>
    <div className="mt-2 grid gap-1 break-all">
      {schemaDrift && <SchemaDriftEvidence evidence={schemaDrift} />}
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

export function WritePublicationSummary({ outputName, destination, admission, outcomeAdmission, receipt, outputs, compact = false, completed = false, publishing = false }: {
  outputName: string; destination: string; admission?: WriteAdmission | null; outcomeAdmission?: WriteAdmission | null; receipt?: WriteReceipt | null; outputs?: RunOutput[]; compact?: boolean; completed?: boolean; publishing?: boolean
}) {
  const classes = compact ? 'mt-2 text-[10.5px]' : 'rounded-md border border-border bg-muted/30 p-2 text-[11px]'
  const summaryAdmission = completed ? outcomeAdmission : admission
  const managed = receipt != null || summaryAdmission?.managed === true
  const providerNeutral = summaryAdmission?.managed === false
  // A receipt can outlive its in-memory admission. Omit an unproven mode instead
  // of filling the primary summary with an "unavailable" implementation state.
  const showMode = summaryAdmission?.mode != null
  const acceptedName = receipt?.name ?? summaryAdmission?.intent?.destination.name
  const displayedName = acceptedName ?? outputName
  const schemaDrift = receipt?.schemaDrift ?? summaryAdmission?.intent?.schemaDrift
  const runtimeSchema = summaryAdmission?.intent?.schemaMode === 'runtime'
  return <section aria-label="Write publication" className={classes}>
    <div className="grid gap-1.5">
      <div>
        <span className="font-semibold text-foreground">
          {managed ? 'Dataset name' : 'Output name'}
        </span>
        <div className="font-mono text-foreground">{displayedName}</div>
      </div>
      <div>
        <span className="font-semibold text-foreground">Destination</span>
        <div className="text-muted-foreground">{destination}</div>
      </div>
      {showMode && <div>
          <span className="font-semibold text-foreground">Mode</span>
          <div className="text-muted-foreground">{managed ? publicationMode(summaryAdmission?.mode) : writeMode(summaryAdmission?.mode)}</div>
        </div>}
      {summaryAdmission?.exactRunReadiness?.ready === false ? <div aria-label="Exact run readiness" role="alert" className="rounded border border-destructive/30 bg-destructive/10 px-2 py-1 text-destructive">
        <strong>Fix before running:</strong> {summaryAdmission.exactRunReadiness.message}
      </div> : summaryAdmission?.blocker ? <div aria-label="Write blocker" role="alert" className="rounded border border-destructive/30 bg-destructive/10 px-2 py-1 text-destructive">
        <strong>Fix before running:</strong> {summaryAdmission.blocker}
      </div> : receipt ? null
        : completed ? <div aria-label="Write readiness" role="status" className="text-muted-foreground">
            {providerNeutral ? 'Run finished. The selected backend wrote the output.' : 'Run finished, but the published dataset could not be confirmed.'}
          </div>
        : publishing ? <div aria-label="Write readiness" role="status" className="text-primary">Writing output…</div>
        : schemaDrift?.requiresConfirmation ? <div aria-label="Write readiness" className="text-amber-700 dark:text-amber-300">
            Review schema changes before running.
          </div>
        : summaryAdmission ? <div aria-label="Write readiness" className="text-emerald-700 dark:text-emerald-300">
            {runtimeSchema
              ? 'Ready to run. Output columns will be checked during the run.'
              : 'Ready to run'}
          </div>
        : <div aria-label="Write readiness" className="text-muted-foreground">Checking output…</div>}
      {receipt && <div aria-label="Published result" className="rounded border border-emerald-500/30 bg-emerald-500/5 px-2 py-1.5 text-foreground">
        <div><strong>Output published</strong></div>
        <div><span className="font-mono">{displayedName}</span> · version <span className="font-mono">{receipt.revisionId}</span> · {receipt.rows.toLocaleString()} rows</div>
        <ExactRevisionAction key={`${receipt.datasetId}:${receipt.revisionId}`} receipt={receipt} />
      </div>}
    </div>
    <PublicationDetails admission={admission} outcomeAdmission={outcomeAdmission} receipt={receipt}
      schemaDrift={schemaDrift} outputs={outputs} />
  </section>
}
