import type { SampleProvenance, SampleResult } from '../types/api'

type PreviewUnit = 'rows' | 'points' | 'groups'
type PreviewSurface = 'canvas' | 'catalog'

export function editorInputFitsPreviewCap(data: SampleResult): boolean {
  const input = data.editorTestInput
  // `rows` is server-proved only when the retained output has an exact materialized count.
  // An absent count is uncertainty, not evidence that the source cap did not apply.
  return input?.rows != null && data.rowLimit != null && input.rows <= data.rowLimit
}

export function previewRangeLabel(unit: PreviewUnit, offset: number, count: number) {
  if (count === 0) return offset === 0 ? `No ${unit} returned` : `No ${unit} at offset ${offset.toLocaleString()}`
  return `${unit} ${(offset + 1).toLocaleString()}–${(offset + count).toLocaleString()}`
}

export function previewWarning(
  data: SampleResult,
  unit: PreviewUnit,
  surface: PreviewSurface = 'canvas',
  suppressSourceCapWarning = false,
): string | null {
  const end = data.rows.length
  const sourceCapped = data.limitScope === 'each-source' || data.limitReason === 'preview-scan'
  if (sourceCapped && !suppressSourceCapWarning) {
    return `Preview uses up to ${(data.rowLimit ?? end).toLocaleString()} ${unit} from each input; output may differ from a full run.`
  }
  const resultCapped = data.limitScope === 'result-window'
    || data.limitReason === 'interactive-row-budget'
    || (data.completeness === 'capped' && !sourceCapped)
  if (!resultCapped) return null
  if (surface === 'catalog') {
    return `Preview is limited to ${(data.rowLimit ?? end).toLocaleString()} ${unit}; the dataset may contain more.`
  }
  return `Showing up to ${(data.rowLimit ?? end).toLocaleString()} ${unit}; run the node to inspect the full result.`
}

function samplingSummary(provenance: SampleProvenance) {
  if (provenance.strategy !== 'reservoir') return null
  return `Random sample · ${provenance.returnedRows.toLocaleString()} rows · seed ${provenance.seed ?? 'unknown'}`
}

export function PreviewSummary({
  data,
  offset = 0,
  unit = 'rows',
  showRange = true,
  showWarning = true,
  surface = 'canvas',
  suppressSourceCapWarning = false,
}: {
  data: SampleResult
  offset?: number
  unit?: PreviewUnit
  showRange?: boolean
  showWarning?: boolean
  surface?: PreviewSurface
  suppressSourceCapWarning?: boolean
}) {
  const summary = data.sampleProvenance ? samplingSummary(data.sampleProvenance) : null
  const warning = showWarning ? previewWarning(data, unit, surface, suppressSourceCapWarning) : null
  if (!showRange && !summary && !warning) return null
  return (
    <div role="status" className="border-b border-border bg-muted/30 px-[11px] py-1.5 text-[10.5px] text-muted-foreground">
      <div className="flex flex-wrap items-center gap-1.5">
        {showRange && <span>{previewRangeLabel(unit, offset, data.rows.length)}</span>}
        {summary && <span>{summary}</span>}
      </div>
      {warning && <div className="mt-1 font-medium text-amber-700 dark:text-amber-300">{warning}</div>}
    </div>
  )
}

export function PreviewProvenance({ provenance, stale = false }: {
  provenance?: SampleProvenance | null
  stale?: boolean
}) {
  if (!provenance) return null
  const summary = samplingSummary(provenance)
  return (
    <>
      {summary && <div className="text-[10.5px] text-muted-foreground">{summary}</div>}
      <PreviewDetails provenance={provenance} stale={stale} />
    </>
  )
}

export function PreviewDetails({ provenance, stale = false }: {
  provenance?: SampleProvenance | null
  stale?: boolean
}) {
  if (!provenance) return null
  const counts = `Requested ${provenance.requestedRows.toLocaleString()} rows · scanned ${provenance.scannedRows?.toLocaleString() ?? 'unknown'} · returned ${provenance.returnedRows.toLocaleString()} · total ${provenance.totalRows?.toLocaleString() ?? 'unknown'}.`
  return (
    <details className="text-[10.5px] text-muted-foreground" data-testid="preview-details">
      <summary className="cursor-pointer select-none py-1 font-medium hover:text-foreground">Preview details</summary>
      <div className="space-y-0.5 pb-1">
        {stale && <div>These saved rows are from the version below, not a refreshed preview.</div>}
        <div>{counts}</div>
        {provenance.limitations.map((limitation) => <div key={limitation}>{limitation}</div>)}
      </div>
    </details>
  )
}
