import { datasetViewerHash, type DatasetViewerCanvasReturn } from '../router'
import type {
  RunOutput, WriteAdmission, WriteReceipt,
} from '../types/api'
import { isMeaningfulSchemaChange } from '../lib/schemaCompatibility'

export function publicationMode(mode: WriteAdmission['mode'] | undefined): string {
  if (mode === 'create') return 'Create a new dataset'
  if (mode === 'append') return 'Append to the selected dataset'
  if (mode === 'replace' || mode === 'overwrite') return 'Replace the selected dataset'
  return 'Revision mode is not available yet'
}

function writeMode(mode: WriteAdmission['mode'] | undefined): string {
  if (mode === 'append') return 'Append to output'
  if (mode === 'replace' || mode === 'overwrite') return 'Replace output'
  if (mode === 'create') return 'Create output'
  return 'Write mode is not available yet'
}

function ExactRevisionAction({ receipt, returnToCanvas }: {
  receipt: WriteReceipt
  returnToCanvas?: DatasetViewerCanvasReturn
}) {
  // The receipt already supplies the immutable identity. Navigation keeps it in the URL so reload
  // and browser Back reopen the same viewer; the viewer owns authorization and retention errors.
  return <a
    className="mt-2 inline-flex rounded-md bg-primary px-2.5 py-1.5 text-[10.5px] font-semibold text-primary-foreground shadow-sm"
    href={datasetViewerHash(receipt.datasetId, receipt.revisionId, returnToCanvas)}>
    Open dataset
  </a>
}

export function PublishedDatasetResult({ receipt, name = receipt.name, returnToCanvas }: {
  receipt: WriteReceipt
  name?: string
  returnToCanvas?: DatasetViewerCanvasReturn
}) {
  return <div aria-label="Published result"
    className="rounded border border-emerald-500/30 bg-emerald-500/5 px-2 py-1.5 text-foreground">
    <div><strong>Published</strong> · <span className="font-mono">{name}</span> · {receipt.rows.toLocaleString()} rows</div>
    <ExactRevisionAction key={`${receipt.datasetId}:${receipt.revisionId}`} receipt={receipt}
      returnToCanvas={returnToCanvas} />
  </div>
}

export function WritePublicationSummary({ outputName, destination, admission, outcomeAdmission, receipt, compact = false, completed = false, publishing = false, returnToCanvas }: {
  outputName: string; destination: string; admission?: WriteAdmission | null; outcomeAdmission?: WriteAdmission | null; receipt?: WriteReceipt | null; outputs?: RunOutput[]; compact?: boolean; completed?: boolean; publishing?: boolean
  returnToCanvas?: DatasetViewerCanvasReturn
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
  const schemaChanges = schemaDrift?.compatibility.fields.filter(isMeaningfulSchemaChange) ?? []
  const runtimeSchema = summaryAdmission?.intent?.schemaMode === 'runtime'
  return <section aria-label="Write publication" className={classes}>
    <div className="grid gap-1.5">
      {!receipt && <div>
        <span className="font-semibold text-foreground">
          {managed ? 'Dataset name' : 'Output name'}
        </span>
        <div className="font-mono text-foreground">{displayedName}</div>
      </div>}
      <div>
        <span className="font-semibold text-foreground">Destination</span>
        <div className="text-muted-foreground">{destination}</div>
      </div>
      {showMode && <div>
          <span className="font-semibold text-foreground">Mode</span>
          <div className="text-muted-foreground">{managed ? publicationMode(summaryAdmission?.mode) : writeMode(summaryAdmission?.mode)}</div>
        </div>}
      {schemaDrift && (schemaDrift.requiresConfirmation || schemaChanges.length > 0) && <div
        aria-label="Schema changes" className="rounded border border-amber-300/60 bg-amber-50/60 px-2 py-1.5 text-amber-950 dark:border-amber-700/60 dark:bg-amber-950/30 dark:text-amber-100">
        <span className="font-semibold">Schema changes</span>
        {schemaChanges.length > 0 ? <ul className="mt-1 list-disc pl-4">
          {schemaChanges.slice(0, 8).map((field, index) => <li key={`${field.fieldId ?? field.oldName ?? field.newName}:${index}`}>
            {field.oldName && field.newName && field.oldName !== field.newName
              ? `${field.oldName} → ${field.newName}`
              : field.newName ?? field.oldName ?? 'A column'}: {field.reason.replaceAll('_', ' ')}
          </li>)}
          {schemaChanges.length > 8 ? <li>{schemaChanges.length - 8} more changes</li> : null}
        </ul> : <div className="mt-1">The destination schema could not be confirmed automatically.</div>}
      </div>}
      {summaryAdmission?.exactRunReadiness?.ready === false ? <div aria-label="Run readiness" role="alert" className="rounded border border-destructive/30 bg-destructive/10 px-2 py-1 text-destructive">
        <strong>Fix before running:</strong> {summaryAdmission.exactRunReadiness.message}
      </div> : summaryAdmission?.blocker ? <div aria-label="Write blocker" role="alert" className="rounded border border-destructive/30 bg-destructive/10 px-2 py-1 text-destructive">
        <strong>Fix before running:</strong> {summaryAdmission.blocker}
      </div> : receipt ? null
        : completed ? <div aria-label="Write readiness" role="status" className="text-muted-foreground">
            {providerNeutral ? 'Run finished. Output was written.' : 'Run finished, but the dataset could not be confirmed.'}
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
      {receipt && <PublishedDatasetResult receipt={receipt} name={displayedName}
        returnToCanvas={returnToCanvas} />}
    </div>
  </section>
}
