import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { routeHash } from '../router'
import type { DatasetViewDefinition, DistributionReportBucketExamples, DistributionReportComparison, DistributionReportDocument, DistributionReportEnvelope, DistributionReportEstimate, DistributionReportSection } from '../types/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'

const active = (status: DistributionReportEnvelope['task']['status']) => status === 'queued' || status === 'running'
const reportHash = (reportId: string, compareId?: string) => `#/distribution-reports/${encodeURIComponent(reportId)}${compareId ? `?compare=${encodeURIComponent(compareId)}` : ''}`
const failure = (caught: unknown) => caught instanceof Error ? caught.message : String(caught)
type ReportFailure = { status?: number; code?: string; detail: string }
const reportFailure = (caught: unknown): ReportFailure => {
  const facts = typeof caught === 'object' && caught !== null
    ? caught as { status?: unknown; code?: unknown } : null
  return {
    ...(typeof facts?.status === 'number' ? { status: facts.status } : {}),
    ...(typeof facts?.code === 'string' ? { code: facts.code } : {}),
    detail: failure(caught),
  }
}
const count = (value: number) => value.toLocaleString()
const number = (value: number | null | undefined) => {
  if (value == null) return 'unknown'
  return value.toString()
}
const date = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : 'not recorded'
const utcDate = (value: string | null | undefined) => value
  ? `${new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' }).format(new Date(value))} UTC`
  : 'not recorded'

export function DistributionReportLauncher({ definition }: { definition: DatasetViewDefinition }) {
  const [reports, setReports] = useState<DistributionReportEnvelope[]>([])
  const [estimate, setEstimate] = useState<DistributionReportEstimate | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ReportFailure | null>(null)
  const request = useRef(0)

  const load = async () => {
    const sequence = ++request.current
    setLoading(true); setError(null)
    try {
      const next = await api.distributionReports(definition.id)
      if (sequence === request.current) setReports(next)
    } catch (caught) {
      if (sequence === request.current) setError(reportFailure(caught))
    } finally { if (sequence === request.current) setLoading(false) }
  }
  useEffect(() => { void load(); return () => { request.current += 1 } }, [definition.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async (confirmed: boolean) => {
    setBusy(true); setError(null)
    try {
      const created = await api.submitDistributionReport(definition.id, globalThis.crypto.randomUUID(), confirmed)
      setEstimate(null)
      setReports((current) => [created, ...current.filter((item) => item.reportId !== created.reportId)])
    } catch (caught) { setError(reportFailure(caught)) }
    finally { setBusy(false) }
  }
  const start = async () => {
    setBusy(true); setError(null)
    try {
      const next = await api.estimateDistributionReport(definition.id)
      if (next.needsConfirmation) setEstimate(next)
      else await submit(false)
    } catch (caught) { setError(reportFailure(caught)) }
    finally { setBusy(false) }
  }

  return <section className="grid gap-2 rounded-lg border border-border bg-muted/20 p-3">
    <div className="flex items-center gap-2"><div className="flex-1 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Distribution reports</div>
      <Button size="sm" variant="outline" onClick={() => void start()} disabled={busy}>{busy ? 'Preparing…' : 'Inspect distributions'}</Button></div>
    <p className="text-[10.5px] text-muted-foreground">Creates a bounded retained report for this exact DatasetView; the preview Stats remain separate.</p>
    {estimate && <div role="dialog" aria-label="Confirm distribution report" className="grid gap-2 rounded border border-amber-500/40 bg-amber-500/10 p-2 text-[11px]">
      <strong>Confirmation required</strong>
      <span>{estimate.reason === 'unknown_size' ? 'The retained metadata does not prove the scan size.' : 'This exact view exceeds the confirmation scan threshold.'} Estimated rows: {estimate.estimatedScanRows == null ? 'unknown' : count(estimate.estimatedScanRows)}; bytes: {estimate.estimatedScanBytes == null ? 'unknown' : count(estimate.estimatedScanBytes)}.</span>
      <span>Bounded to {estimate.limits.reportedColumns} columns, {estimate.limits.topCategories} categories and {estimate.limits.histogramBuckets} buckets per section; deadline {estimate.limits.deadlineSeconds}s.</span>
      <div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => setEstimate(null)} disabled={busy}>Cancel</Button><Button size="sm" onClick={() => void submit(true)} disabled={busy}>Confirm and start</Button></div>
    </div>}
    {error && <div role="alert" className="rounded border border-destructive/30 bg-destructive/10 p-2 text-[11px] text-destructive">{prepareMessage(error)} <button className="font-semibold underline" onClick={() => void load()}>Retry list</button></div>}
    {loading ? <span className="text-[11px] text-muted-foreground">Loading retained reports…</span>
      : reports.length === 0 ? <span className="text-[11px] text-muted-foreground">No retained reports yet.</span>
        : <ol className="grid gap-1">{reports.map((item) => <li key={item.reportId} className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded border border-border bg-background px-2 py-1.5 text-[10.5px]">
          <Badge variant="secondary" className="capitalize">{item.task.status}</Badge><span>{item.report ? `${count(item.report.measuredRows)} measured rows · ${item.report.complete ? 'complete' : 'sample'}` : 'No validated report document yet'}</span>
          <a className="font-semibold text-primary underline" href={reportHash(item.reportId)}>Open report</a><span className="ml-auto text-muted-foreground">{date(item.updatedAt)}</span>
        </li>)}</ol>}
  </section>
}

export function DistributionReportPage({ reportId, compareReportId, onClose }: { reportId: string; compareReportId?: string; onClose?: () => void }) {
  const [envelope, setEnvelope] = useState<DistributionReportEnvelope | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshError, setRefreshError] = useState<ReportFailure | null>(null)
  const [actionError, setActionError] = useState('')
  const [acting, setActing] = useState(false)
  const request = useRef(0)
  const actionRequest = useRef(0)
  const reportIdentity = useRef(reportId)
  const retryActions = useRef(new Map<string, string>())
  reportIdentity.current = reportId

  const load = async (targetReportId = reportId, showLoading = false) => {
    const sequence = ++request.current
    if (showLoading) setLoading(true)
    try {
      const next = await api.distributionReport(targetReportId)
      if (sequence === request.current && reportIdentity.current === targetReportId) { setEnvelope(next); setRefreshError(null) }
    } catch (caught) {
      if (sequence === request.current && reportIdentity.current === targetReportId) setRefreshError(reportFailure(caught))
    } finally {
      if (sequence === request.current && reportIdentity.current === targetReportId) setLoading(false)
    }
  }
  useEffect(() => {
    actionRequest.current += 1
    setEnvelope(null); setLoading(true); setRefreshError(null); setActionError(''); setActing(false)
    void load(reportId, true)
    return () => { request.current += 1; actionRequest.current += 1 }
  }, [reportId]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!envelope || !active(envelope.task.status)) return
    const timer = window.setInterval(() => { void load(reportId) }, 1000)
    return () => window.clearInterval(timer)
  }, [reportId, envelope?.task.status]) // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (kind: 'cancel' | 'retry') => {
    if (!envelope) return
    const targetReportId = reportId
    const action = ++actionRequest.current
    setActing(true); setActionError('')
    try {
      if (kind === 'cancel') await api.cancelRun(envelope.task.id)
      else {
        const actionId = retryActions.current.get(envelope.task.id) ?? globalThis.crypto.randomUUID()
        retryActions.current.set(envelope.task.id, actionId)
        await api.retryRun(envelope.task.id, actionId)
        retryActions.current.delete(envelope.task.id)
      }
      if (action === actionRequest.current && reportIdentity.current === targetReportId) await load(targetReportId)
    } catch (caught) {
      if (action === actionRequest.current && reportIdentity.current === targetReportId) setActionError(failure(caught))
    } finally {
      if (action === actionRequest.current && reportIdentity.current === targetReportId) setActing(false)
    }
  }

  if (loading && !envelope) return <div className="p-6 text-[12px] text-muted-foreground">Loading exact retained report…</div>
  if (!envelope) return <div role="alert" className="m-4 rounded border border-destructive/30 bg-destructive/10 p-4 text-[12px] text-destructive">{unavailableMessage(refreshError)} <button className="font-semibold underline" onClick={() => void load(reportId, true)}>Retry</button></div>
  const { task, report, viewSnapshot } = envelope
  return <div className="mx-auto grid max-w-6xl gap-4 p-4 sm:p-7">
    <header className="flex flex-wrap items-start gap-3 border-b border-border pb-4"><div className="min-w-0 flex-1"><div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Retained distribution report</div><h1 className="text-[20px] font-bold">{viewSnapshot.name}</h1><div className="mt-1 flex flex-wrap gap-2 text-[11px] text-muted-foreground"><Badge variant="secondary" className="capitalize">{task.status}</Badge><span>Updated {date(envelope.updatedAt)}</span>{task.progress != null && <span>{Math.round(task.progress * 100)}% complete</span>}</div></div>
      <a className="rounded-md border border-border bg-background px-2 py-1 text-[11px] font-semibold hover:bg-accent" href={routeHash('jobs', undefined, undefined, undefined, `run=${encodeURIComponent(task.id)}`)}>Open Jobs</a>
      {onClose && <Button size="sm" variant="outline" onClick={onClose}>Close</Button>}</header>
    {refreshError && <div role="alert" className="rounded border border-destructive/30 bg-destructive/10 p-3 text-[11px] text-destructive">{refreshMessage(refreshError)} Showing the last validated report. <button className="font-semibold underline" onClick={() => void load(reportId)}>Retry</button></div>}
    {actionError && <div role="alert" className="rounded border border-destructive/30 bg-destructive/10 p-3 text-[11px] text-destructive">Report action failed: {actionError}</div>}
    {!report && <section className="grid gap-2 rounded-lg border border-border bg-muted/20 p-4 text-[12px]"><strong>{task.status === 'failed' ? task.attempts.length >= task.maxAttempts ? 'Report failed and retries are exhausted before a validated document was retained.' : 'Report failed before a validated document was retained.' : task.status === 'cancelled' ? 'Report was cancelled before a validated document was retained.' : 'Computing exact retained report…'}</strong><span className="text-muted-foreground">Attempt {task.attempts.length} of {task.maxAttempts}{task.cancelRequested ? ' · cancellation requested' : ''}.</span>{task.error && <span role="alert" className="text-destructive">{taskErrorMessage(task.error)}</span>}<Actions task={task} busy={acting} onAction={act} /></section>}
    {report && <ReportPresentation key={reportId} report={report} view={viewSnapshot} compareReportId={compareReportId} />}
  </div>
}

function Actions({ task, busy, onAction }: { task: DistributionReportEnvelope['task']; busy: boolean; onAction: (kind: 'cancel' | 'retry') => void }) {
  const canRetry = (task.status === 'failed' || task.status === 'cancelled') && task.attempts.length < task.maxAttempts
  return <div className="flex gap-2">{active(task.status) && <Button size="sm" variant="outline" disabled={busy || task.cancelRequested} onClick={() => onAction('cancel')}>{task.cancelRequested ? 'Cancellation requested' : 'Cancel report'}</Button>}{canRetry && <Button size="sm" variant="outline" disabled={busy} onClick={() => onAction('retry')}>Retry report</Button>}</div>
}

type ExampleTarget = { reportId: string; sectionId: string; bucketId: string; bucketKind: 'numeric' | 'categorical' | 'temporal'; bucketLabel: string }

function ReportPresentation({ report, view, compareReportId }: { report: DistributionReportDocument; view: DatasetViewDefinition; compareReportId?: string }) {
  const [reports, setReports] = useState<DistributionReportEnvelope[]>([])
  const [comparison, setComparison] = useState<DistributionReportComparison | null>(null)
  const [comparisonError, setComparisonError] = useState<ReportFailure | null>(null)
  const [drawer, setDrawer] = useState<ExampleTarget | null>(null)
  const [examples, setExamples] = useState<DistributionReportBucketExamples | null>(null)
  const [examplesError, setExamplesError] = useState<ReportFailure | null>(null)
  const [comparisonRetry, setComparisonRetry] = useState(0)
  const comparisonRequest = useRef(0)
  const examplesRequest = useRef(0)
  const identity = `${report.reportId}:${compareReportId ?? ''}`

  useEffect(() => {
    let live = true
    void (async () => {
      try {
        const next = await api.distributionReports(report.datasetViewId)
        if (live) setReports(next.filter((item) => item.task.status === 'done' && item.report != null))
      } catch { /* the selector is optional presentation */ }
    })()
    return () => { live = false }
  }, [report.datasetViewId])
  useEffect(() => {
    const sequence = ++comparisonRequest.current
    setComparison(null); setComparisonError(null); setDrawer(null); setExamples(null); setExamplesError(null)
    if (!compareReportId) return
    void api.compareDistributionReports(report.reportId, compareReportId).then((next) => {
      if (sequence === comparisonRequest.current) setComparison(next)
    }).catch((caught) => {
      if (sequence === comparisonRequest.current) setComparisonError(reportFailure(caught))
    })
  }, [identity, report.reportId, compareReportId, comparisonRetry])
  const openExamples = (target: ExampleTarget) => {
    const sequence = ++examplesRequest.current
    setDrawer(target); setExamples(null); setExamplesError(null)
    void api.distributionReportBucketExamples(target.reportId, target.sectionId, target.bucketId).then((next) => {
      if (sequence === examplesRequest.current) setExamples(next)
    }).catch((caught) => {
      if (sequence === examplesRequest.current) setExamplesError(reportFailure(caught))
    })
  }
  return <>
    <section className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-muted/20 p-3 text-[11px]">
      <label htmlFor="compare-retained-report" className="font-semibold">Compare with retained report</label>
      <select id="compare-retained-report" value={compareReportId ?? ''} onChange={(event) => { location.hash = reportHash(report.reportId, event.target.value || undefined) }} className="min-w-64 rounded border border-border bg-background px-2 py-1">
        <option value="">No comparison</option>
        {compareReportId && !reports.some((item) => item.reportId === compareReportId) && <option value={compareReportId}>{comparison ? `Linked report · ${comparison.coverage.right.datasetId}@${comparison.coverage.right.revisionId}` : `Linked report · ${compareReportId}`}</option>}
        {reports.filter((item) => item.reportId !== report.reportId).map((item) => <option key={item.reportId} value={item.reportId}>{item.viewSnapshot.name} · {item.report ? `${count(item.report.measuredRows)} measured` : 'retained report'}</option>)}
      </select><span className="text-muted-foreground">Completed reports for this DatasetView only.</span>
    </section>
    {!compareReportId && <ReportDocument report={report} view={view} onExamples={openExamples} />}
    {compareReportId && !comparison && !comparisonError && <><span className="text-[11px] text-muted-foreground">Loading comparison…</span><ReportDocument report={report} view={view} onExamples={openExamples} /></>}
    {compareReportId && comparisonError && <><div role="alert" className="rounded border border-destructive/30 bg-destructive/10 p-3 text-[11px] text-destructive">{comparisonMessage(comparisonError)} <button className="font-semibold underline" onClick={() => setComparisonRetry((value) => value + 1)}>Retry</button></div><ReportDocument report={report} view={view} onExamples={openExamples} /></>}
    {comparison && <ComparisonDocument comparison={comparison} onExamples={openExamples} />}
    {drawer && <ExamplesDrawer target={drawer} examples={examples} error={examplesError} onClose={() => { examplesRequest.current += 1; setDrawer(null) }} onRetry={() => openExamples(drawer)} />}
  </>
}

function ReportDocument({ report, view, onExamples }: { report: DistributionReportDocument; view: DatasetViewDefinition; onExamples: (target: ExampleTarget) => void }) {
  const coverage = report.sections.find((section): section is Extract<DistributionReportSection, { kind: 'coverage_schema' }> => section.kind === 'coverage_schema')
  const sections = report.sections.filter((section) => section.kind !== 'coverage_schema')
  const sampling = view.sampling.kind === 'all' ? 'All matching rows' : `Reservoir sample of ${count(view.sampling.size)} rows · seed ${view.sampling.seed}`
  return <><Evidence report={report} view={view} coverage={coverage} sampling={sampling} />
    <div className="grid gap-3">{sections.map((section) => <Section key={section.sectionId} section={section} measuredRows={report.measuredRows} reportId={report.reportId} onExamples={onExamples} />)}</div></>
}

function Evidence({ report, view, coverage, sampling }: { report: DistributionReportDocument; view: DatasetViewDefinition; coverage: Extract<DistributionReportSection, { kind: 'coverage_schema' }> | undefined; sampling: string }) {
  return <section className="grid gap-2 rounded-lg border border-border bg-muted/20 p-4 text-[11px]"><div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Coverage before distributions</div>
    <div className="grid gap-1 sm:grid-cols-2"><div><strong>Dataset / revision:</strong> <span className="font-mono break-all">{report.datasetId}@{report.revisionId}</span></div><div><strong>View:</strong> {view.name} · <span className="font-mono">{report.viewDefinitionSha256}</span></div><div><strong>Population:</strong> {sampling}</div><div><strong>Measured:</strong> {count(report.measuredRows)} rows · {report.complete ? 'complete for this view' : 'sample only; no full-population claim'}</div><div><strong>Columns:</strong> {coverage ? `${coverage.reportedColumnCount} of ${coverage.selectedColumnCount} selected` : 'coverage document unavailable'}</div><div><strong>Computation:</strong> {report.computationVersion}</div></div>
    {report.sampleProvenance && <div className="rounded border border-border bg-background p-2"><strong>Sample evidence:</strong> {count(report.sampleProvenance.returnedRows)} returned{report.sampleProvenance.totalRows != null ? ` of ${count(report.sampleProvenance.totalRows)}` : ' of unknown total'} · scanned {report.sampleProvenance.scannedRows == null ? 'unknown' : count(report.sampleProvenance.scannedRows)} · {report.sampleProvenance.strategy}{report.sampleProvenance.seed != null ? ` · seed ${report.sampleProvenance.seed}` : ''}</div>}
    {report.limitations.length > 0 && <ul className="list-disc pl-4 text-muted-foreground">{report.limitations.map((item) => <li key={item}>{item}</li>)}</ul>}
  </section>
}

function ComparisonDocument({ comparison, onExamples }: { comparison: DistributionReportComparison; onExamples: (target: ExampleTarget) => void }) {
  const { coverage } = comparison
  return <>
    <section className="grid gap-2 rounded-lg border border-border bg-muted/20 p-4 text-[11px]">
      <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Coverage and identity before comparison</div>
      <div className="grid gap-2 sm:grid-cols-2"><Identity label="Current report" identity={coverage.left} /><Identity label="Comparison report" identity={coverage.right} /></div>
      <strong>{coverage.comparable ? 'Coverage is comparable.' : `Coverage is not comparable: ${coverageReason(coverage.reason)}.`}</strong>
    </section>
    <div className="grid gap-3">{comparison.columns.map((column) => {
      const authorized = coverage.comparable && column.comparable
      return <section key={`${column.leftColumn.name}:${column.rightColumn.name}`} className="grid gap-3 rounded-lg border border-border p-3 text-[11px]">
        <div><strong>{column.leftColumn.name}</strong><span className="ml-2 text-muted-foreground">Matched by {column.matchReason.replaceAll('_', ' ')}</span></div>
        {!authorized && <span className="text-amber-800">No deltas: {column.reason.replaceAll('_', ' ')}.</span>}
        {authorized && <Delta column={column} />}
        <div className="grid gap-3 lg:grid-cols-2"><div className="grid gap-2"><strong>Current report</strong>{column.leftSections.map((section) => <Section key={section.sectionId} section={section} measuredRows={coverage.left.measuredRows} reportId={coverage.left.reportId} onExamples={onExamples} />)}</div><div className="grid gap-2"><strong>Comparison report</strong>{column.rightSections.map((section) => <Section key={section.sectionId} section={section} measuredRows={coverage.right.measuredRows} reportId={coverage.right.reportId} onExamples={onExamples} />)}</div></div>
      </section>
    })}</div>
    {(comparison.unmatchedLeftColumns.length > 0 || comparison.unmatchedRightColumns.length > 0) && <section className="grid gap-1 rounded-lg border border-border p-3 text-[11px]"><strong>Unmatched columns remain unpaired</strong>{comparison.unmatchedLeftColumns.length > 0 && <span>Current only: {comparison.unmatchedLeftColumns.map((column) => column.name).join(', ')}</span>}{comparison.unmatchedRightColumns.length > 0 && <span>Comparison only: {comparison.unmatchedRightColumns.map((column) => column.name).join(', ')}</span>}</section>}
  </>
}

function Identity({ label, identity }: { label: string; identity: DistributionReportComparison['coverage']['left'] }) {
  return <div><strong>{label}</strong><div className="font-mono break-all">{identity.datasetId}@{identity.revisionId}</div><div>{count(identity.measuredRows)} measured · {identity.complete ? 'complete' : 'sample'} · {identity.computationVersion}</div><div className="break-all">Sample identity: {identity.samplingIdentity}</div></div>
}

function Delta({ column }: { column: DistributionReportComparison['columns'][number] }) {
  const delta = column.metricDelta
  return <div className="rounded border border-primary/30 bg-primary/5 p-2"><strong>Server-authorized deltas (comparison − current)</strong>{column.missingCountDelta != null && <div>Missing: {signed(column.missingCountDelta)}</div>}{delta?.kind === 'numeric' && <><div>Finite: {signed(delta.countDelta)} · non-finite: {signed(delta.nonFiniteCountDelta)}</div><div>Min: {signed(delta.minDelta)} · max: {signed(delta.maxDelta)} · mean: {signed(delta.meanDelta)} · sd: {signed(delta.stddevDelta)}</div><div>Quantiles: {delta.quantiles.map((item) => `p${Math.round(item.probability * 100)} ${signed(item.valueDelta)}`).join(' · ')}</div><div>{delta.histogramReason === 'unequal_edges' ? 'Histogram bucket edges differ; distributions are shown side by side.' : `Histogram bucket deltas: ${delta.histogram?.map((bucket) => signed(bucket.countDelta)).join(', ') || 'none'}`}</div></>}{delta?.kind === 'temporal' && <div>{delta.bucketReason === 'unequal_edges' ? 'Temporal bucket edges differ; distributions are shown side by side.' : `Temporal bucket deltas: ${delta.buckets?.map((bucket) => signed(bucket.countDelta)).join(', ') || 'none'}`}</div>}{delta?.kind === 'categorical' && <div className="grid gap-1">{delta.categories.map((category) => <span key={`${typeof category.label}:${category.label}`}>{String(category.label)}: {category.leftCount == null ? 'outside current top-K' : count(category.leftCount)} / {category.rightCount == null ? 'outside comparison top-K' : count(category.rightCount)}{category.countDelta != null ? ` · ${signed(category.countDelta)}` : ''}</span>)}<span>Other: {delta.otherCountReason.replaceAll('_', ' ')}{delta.otherCountDelta != null ? ` · ${signed(delta.otherCountDelta)}` : ''}; distinct: {delta.distinctCountReason.replaceAll('_', ' ')}{delta.distinctCountDelta != null ? ` · ${signed(delta.distinctCountDelta)}` : ''}</span></div>}</div>
}

function Section({ section, measuredRows, reportId, onExamples }: { section: DistributionReportSection; measuredRows: number; reportId: string; onExamples: (target: ExampleTarget) => void }) {
  if (section.kind === 'coverage_schema') return null
  if (section.kind === 'missingness') return <section className="rounded-lg border border-border p-3 text-[11px]"><strong>{section.columnName}</strong><span className="ml-2 text-muted-foreground">Missingness</span><div className="mt-2"><Bar value={section.missingCount} total={measuredRows} /><span>{count(section.missingCount)} null{section.missingCount === 1 ? '' : 's'} of {count(measuredRows)} measured rows</span></div></section>
  if (section.kind === 'unsupported') return <section className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-[11px]"><strong>{section.columnName ?? 'Additional columns'}</strong><span className="ml-2 text-amber-800">Unsupported / partial: {section.reason.replaceAll('_', ' ')}</span>{section.omittedCount != null && <div className="mt-1">{count(section.omittedCount)} columns omitted by the report bound.</div>}</section>
  if (section.kind === 'numeric') return <section className="grid gap-2 rounded-lg border border-border p-3 text-[11px]"><strong>{section.columnName}</strong><div className="grid gap-1 text-muted-foreground sm:grid-cols-4"><span>Finite {count(section.count)}</span><span>Non-finite {count(section.nonFiniteCount)}</span><span>Min / max {number(section.min)} / {number(section.max)}</span><span>Mean / sd {number(section.mean)} / {number(section.stddev)}</span></div><Bars items={section.histogram.map((bucket) => ({ key: bucket.bucketId, label: `${number(bucket.lower)}–${number(bucket.upper)}${bucket.upperInclusive ? ']' : ')'}`, count: bucket.count }))} onExample={(bucketId, bucketLabel) => onExamples({ reportId, sectionId: section.sectionId, bucketId, bucketKind: 'numeric', bucketLabel })} /><div className="text-muted-foreground">Quantiles: {section.quantiles.map((item) => `p${Math.round(item.probability * 100)} ${number(item.value)}`).join(' · ')}</div></section>
  if (section.kind === 'categorical') return <section className="grid gap-2 rounded-lg border border-border p-3 text-[11px]"><strong>{section.columnName}</strong><span className="text-muted-foreground">{section.distinctCountApproximate ? 'Approximate' : 'Exact'} distinct count: {count(section.distinctCount)}</span><Bars items={[...section.top.map((item) => ({ key: item.bucketId, label: `Value: ${String(item.label)}`, count: item.count })), { key: `${section.sectionId}:remainder`, label: 'Other (top-K remainder)', count: section.otherCount, example: false }]} onExample={(bucketId, bucketLabel) => onExamples({ reportId, sectionId: section.sectionId, bucketId, bucketKind: 'categorical', bucketLabel })} /></section>
  return <section className="grid gap-2 rounded-lg border border-border p-3 text-[11px]"><strong>{section.columnName}</strong><span className="text-muted-foreground">UTC range: {utcDate(section.min)} – {utcDate(section.max)}</span><Bars items={section.buckets.map((bucket) => ({ key: bucket.bucketId, label: `${utcDate(bucket.start)} – ${utcDate(bucket.end)}${bucket.endInclusive ? ' inclusive' : ''}`, count: bucket.count }))} onExample={(bucketId, bucketLabel) => onExamples({ reportId, sectionId: section.sectionId, bucketId, bucketKind: 'temporal', bucketLabel })} /></section>
}

function Bars({ items, onExample }: { items: Array<{ key?: string; label: string; count: number; example?: boolean }>; onExample?: (bucketId: string, bucketLabel: string) => void }) {
  const largest = Math.max(1, ...items.map((item) => item.count))
  return <div className="grid gap-1">{items.map((item) => <div key={item.key ?? item.label} className="grid grid-cols-[minmax(90px,1fr)_minmax(80px,2fr)_auto] items-center gap-2"><span className="truncate" title={item.label}>{item.label}</span><Bar value={item.count} total={largest} /><span className="font-mono text-muted-foreground">{count(item.count)}</span>{onExample && item.key && item.example !== false && <button className="text-primary underline" onClick={() => onExample(item.key!, item.label)}>View examples</button>}</div>)}</div>
}

function Bar({ value, total }: { value: number; total: number }) {
  return <span className="h-2 overflow-hidden rounded bg-muted" aria-label={`${count(value)} of ${count(total)} rows`}><span className={`block h-full rounded bg-primary${value > 0 ? ' min-w-px' : ''}`} style={{ width: `${Math.min(100, total ? value / total * 100 : 0)}%` }} /></span>
}

function ExamplesDrawer({ target, examples, error, onClose, onRetry }: { target: ExampleTarget; examples: DistributionReportBucketExamples | null; error: ReportFailure | null; onClose: () => void; onRetry: () => void }) {
  return <div className="fixed inset-0 z-50 flex justify-end bg-black/20" onMouseDown={onClose}><section role="dialog" aria-modal="true" aria-label="Bucket examples" className="grid h-full w-[520px] max-w-full content-start gap-3 overflow-auto border-l border-border bg-card p-5 shadow-xl" onMouseDown={(event) => event.stopPropagation()}><div className="flex items-center gap-2"><strong className="flex-1">Examples from measured bucket</strong><Button size="sm" variant="outline" onClick={onClose}>Close</Button></div><span className="text-[11px] text-muted-foreground">{target.bucketKind} bucket: {target.bucketLabel} · <span className="font-mono">{target.sectionId}/{target.bucketId}</span></span>{!examples && !error && <span className="text-[11px] text-muted-foreground">Loading bounded examples…</span>}{error && <div role="alert" className="text-[11px] text-destructive">{examplesMessage(error)} <button className="font-semibold underline" onClick={onRetry}>Retry</button></div>}{examples && <><div className="grid gap-1 text-[11px]"><span><strong>Report:</strong> <span className="font-mono">{examples.reportId}</span></span><span><strong>DatasetView:</strong> {examples.datasetViewId}</span><span><strong>Exact revision:</strong> <span className="font-mono">{examples.datasetId}@{examples.revisionId}</span></span><span><strong>Bucket:</strong> {examples.bucketKind} · {examples.columnName} · {count(examples.bucketCount)} measured rows · sample {examples.samplingIdentity}</span><span>{examples.exampleSemantics.replaceAll('_', ' ')}; {examples.returnedRows} of {examples.rowLimit} returned{examples.truncated ? ' (truncated)' : ''}.</span></div>{examples.rows.length === 0 ? <span className="text-[11px] text-muted-foreground">This measured bucket has no available example rows.</span> : <pre className="overflow-auto rounded border border-border bg-muted/20 p-2 text-[10px]">{JSON.stringify(examples.rows, null, 2)}</pre>}</>}</section></div>
}

const signed = (value: number | null | undefined) => value == null ? 'unavailable' : `${value > 0 ? '+' : ''}${value}`
function coverageReason(reason: DistributionReportComparison['coverage']['reason']) { return reason === 'full_sample_coverage_mismatch' ? 'one report is full coverage and the other is sampled' : reason === 'different_deterministic_samples' ? 'the reports use different deterministic samples' : reason.replaceAll('_', ' ') }
function comparisonMessage(error: ReportFailure) { if (error.status === 401 || error.status === 403 || error.status === 404 || error.code === 'permission_denied' || error.code === 'not_found') return 'The selected comparison is unavailable or not authorized.'; if (error.status === 503 || error.code === 'service_unavailable') return 'Comparison is temporarily unavailable.'; if (error.status === 422) return 'The selected reports cannot be compared.'; return 'Comparison is currently unavailable.' }
function examplesMessage(error: ReportFailure) { if (error.status === 410 || error.code === 'resource_gone') return 'Examples are unavailable because this exact revision is no longer available.'; if (error.status === 401 || error.status === 403 || error.status === 404 || error.code === 'permission_denied' || error.code === 'not_found') return 'Examples are unavailable for this bucket.'; if (error.status === 422) return 'This bucket is unsupported or no longer valid for the retained report.'; if (error.status === 503 || error.code === 'service_unavailable') return 'Examples are temporarily unavailable.'; return 'Examples are currently unavailable.' }

function unavailableMessage(error: ReportFailure | null): string {
  if (error?.code === 'permission_denied' || error?.status === 401 || error?.status === 403) return 'You are not authorized to open this retained report.'
  if (error?.code === 'resource_gone' || error?.status === 410) return 'The exact retained revision required by this report is no longer available.'
  if (error?.code === 'internal_error') return 'The retained report state could not be validated because it is corrupt.'
  if (error?.code === 'service_unavailable' || error?.status != null && error.status >= 500) return 'The retained report service is temporarily unavailable.'
  if (error?.code === 'not_found' || error?.status === 404) return 'This retained report does not exist, was deleted, or is not visible to you.'
  return 'This retained report is currently unavailable.'
}

function prepareMessage(error: ReportFailure): string {
  if (error.code === 'permission_denied' || error.status === 401 || error.status === 403) return 'You are not authorized to inspect distributions for this exact view.'
  if (error.code === 'resource_gone' || error.status === 410) return 'The exact retained revision is no longer available; no new report was started.'
  if (error.code === 'internal_error') return 'The retained report state failed server validation; no new report was started.'
  if (error.code === 'not_found' || error.status === 404) return 'This exact view no longer exists or is not visible to you.'
  return `Couldn’t prepare the report: ${error.detail}`
}

function refreshMessage(error: ReportFailure): string {
  if (error.code === 'permission_denied' || error.status === 401 || error.status === 403) return 'Permission to refresh this retained report was lost.'
  if (error.code === 'resource_gone' || error.status === 410) return 'The exact retained revision is no longer available for refresh.'
  if (error.code === 'internal_error') return 'The retained report state failed validation during refresh.'
  return `Couldn’t refresh this report: ${error.detail}.`
}

function taskErrorMessage(error: string): string {
  if (error === 'distribution report revision unavailable') return 'The exact retained revision became unavailable before this report finished.'
  if (error === 'distribution report snapshot invalid') return 'The retained report state failed validation before computation finished.'
  if (error === 'distribution report deadline') return 'Report computation exceeded its bounded deadline.'
  if (error === 'distribution report computation failed') return 'Report computation failed before a validated document was retained.'
  return error
}
