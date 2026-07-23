import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from '../api/client'
import type { CatalogTable } from '../types/api'
import type { ColumnSchema, TypedRowReference } from '../types/graph'
import { Popover } from '../ui/Popover'

const message = (error: unknown) => error instanceof Error ? error.message : String(error)
const status = (error: unknown) => typeof error === 'object' && error !== null
  ? (error as { status?: unknown }).status : undefined

function fact(value: string | boolean | null | undefined, unknown = 'not supplied') {
  if (value == null) return unknown
  return typeof value === 'boolean' ? (value ? 'yes' : 'no') : value
}

function retainedTarget(reference: TypedRowReference) {
  const { target } = reference
  return target.kind === 'exact'
    ? [`dataset:${target.datasetId}`, `revision:${target.revisionId}`]
    : [`dataset:${target.datasetId}`, 'revision:not supplied']
}

function unavailableTarget(error: unknown) {
  const code = status(error)
  if (code === 403) return 'Target catalog identity is unavailable: access is denied.'
  if (code === 404 || code === 410) return 'Target catalog identity is unavailable; no current dataset was substituted.'
  return `Target catalog identity could not be resolved: ${message(error)}`
}

/**
 * One compact trigger for the bounded, sanitized metadata contract on a field.  The raw source
 * adapter state never reaches this component: it renders only ColumnSchema annotations supplied by
 * the API contract, then resolves a reference target separately and only after the user asks.
 */
export function FieldEvidenceButton({ column, className, label, marker = false }: {
  column: ColumnSchema
  className?: string
  label?: string
  marker?: boolean
}) {
  const [open, setOpen] = useState(false)
  const anchor = useRef<HTMLButtonElement>(null)
  const hasEvidence = !!column.rowReference || !!column.annotations?.length
  return <>
    <button ref={anchor} type="button" onClick={(event) => { event.stopPropagation(); setOpen((value) => !value) }}
      aria-expanded={open} aria-label={`Inspect evidence for ${column.name}`}
      title={hasEvidence ? `Inspect field evidence for ${column.name}` : `Inspect field details for ${column.name}`}
      className={className ?? 'rounded px-1 text-left hover:bg-accent'}>
      {marker && column.rowReference ? <span aria-hidden="true" className="mr-1 text-primary">↗</span> : null}
      {label ?? column.name}
    </button>
    <Popover anchorRef={anchor} open={open} onClose={() => setOpen(false)} width={390} placement="top" maxHeight={520}>
      <FieldEvidenceContent column={column} />
    </Popover>
  </>
}

export function FieldEvidenceContent({ column }: { column: ColumnSchema }) {
  const reference = column.rowReference
  const [target, setTarget] = useState<CatalogTable | null>(null)
  const [targetState, setTargetState] = useState<'idle' | 'loading' | 'unavailable'>('idle')
  const [targetError, setTargetError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setTarget(null); setTargetError(null)
    if (!reference) { setTargetState('idle'); return () => { live = false } }
    setTargetState('loading')
    // A row reference names a retained catalog identity, never a display-name/current-head lookup.
    // `table()` may resolve a stale logical id to another current registration; keep this strict.
    void api.tableByRegistration(reference.target.datasetId).then((resolved) => {
      if (!live) return
      setTarget(resolved); setTargetState('idle')
    }).catch((error) => {
      if (!live) return
      setTargetState('unavailable'); setTargetError(unavailableTarget(error))
    })
    return () => { live = false }
  }, [reference?.target.datasetId, reference?.target.kind, reference?.target.kind === 'exact' ? reference.target.revisionId : undefined])

  return <div data-testid={`field-evidence-${column.name}`} className="grid gap-3 p-2 text-[10.5px]">
    <div>
      <div className="dp-mono break-all text-[12px] font-semibold text-foreground">{column.name}</div>
      <div className="text-muted-foreground">Field detail · API-sanitized evidence only</div>
    </div>

    <EvidenceSection title="Schema">
      <Facts values={[
        ['Logical type', column.type], ['Physical type', fact(column.physicalType)],
        ['Nullable', fact(column.nullable)], ['Has default', fact(column.hasDefault)],
        ['Stable field identity', fact(column.fieldId)], ['Schema provenance', fact(column.provenance, 'unknown')],
      ]} />
    </EvidenceSection>

    <EvidenceSection title="Row-reference target">
      {!reference ? <div className="text-muted-foreground">No row-reference target was supplied.</div> : <>
        <Facts values={[
          ['Retained identity', retainedTarget(reference).join(' · ')],
          ['Target key', reference.keyFields.join(', ')], ['Evidence', reference.provenance],
          ['Semantic type', fact(reference.semanticType)],
        ]} />
        {targetState === 'loading' && <div role="status" className="text-muted-foreground">Resolving current catalog display…</div>}
        {targetState === 'unavailable' && <div role="alert" className="rounded border border-destructive/30 bg-destructive/5 p-1.5 text-destructive">{targetError}</div>}
        {target && <div className="rounded border border-border bg-muted/30 p-1.5">
          <div>Current catalog display: <strong>{target.name}</strong></div>
          <div className="mt-0.5 break-all font-mono text-[9.5px]">catalog:{target.registrationId ?? target.id}</div>
          <a href={`#/workspace/${encodeURIComponent(`dataset:${target.registrationId ?? target.id}`)}`} className="mt-1 inline-block font-semibold text-primary underline">Open current catalog entry</a>
          <div className="mt-1 text-[9.5px] text-muted-foreground">The retained reference above is not replaced by this current display.</div>
        </div>}
      </>}
    </EvidenceSection>

    <EvidenceSection title="Raw annotations">
      {!column.annotations?.length ? <div className="text-muted-foreground">No safe raw annotations were supplied. Values excluded by the adapter redaction contract are not exposed here.</div>
        : <div className="grid gap-1.5">{column.annotations.map((annotation) => <div key={annotation.key} className="rounded border border-border bg-muted/20 p-1.5">
          <div className="flex flex-wrap gap-x-2 text-[9.5px] text-muted-foreground"><span>{annotation.provenance}</span><span>{annotation.encoding}</span></div>
          <div className="mt-0.5 break-all font-mono font-semibold text-foreground">{annotation.key}</div>
          <div className="max-h-24 overflow-auto break-all font-mono text-[9.5px] text-foreground">{annotation.value}</div>
        </div>)}</div>}
    </EvidenceSection>
  </div>
}

function EvidenceSection({ title, children }: { title: string; children: ReactNode }) {
  return <section className="grid gap-1.5"><div className="text-[9px] font-bold uppercase tracking-wide text-muted-foreground">{title}</div>{children}</section>
}

function Facts({ values }: { values: Array<[string, string]> }) {
  return <dl className="grid grid-cols-[120px_minmax(0,1fr)] gap-x-2 gap-y-1">
    {values.map(([label, value]) => <Fragment key={label}><dt className="text-muted-foreground">{label}</dt><dd className="break-all font-mono text-foreground">{value}</dd></Fragment>)}
  </dl>
}
