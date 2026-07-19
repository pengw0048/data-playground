import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { routeHash } from '../router'
import type { DatasetViewDefinition, DistributionReportDocument, DistributionReportEnvelope, DistributionReportEstimate, DistributionReportSection } from '../types/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'

const active = (status: DistributionReportEnvelope['task']['status']) => status === 'queued' || status === 'running'
const reportHash = (reportId: string) => `#/distribution-reports/${encodeURIComponent(reportId)}`
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

export function DistributionReportPage({ reportId, onClose }: { reportId: string; onClose?: () => void }) {
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
    {report && <ReportDocument report={report} view={viewSnapshot} />}
  </div>
}

function Actions({ task, busy, onAction }: { task: DistributionReportEnvelope['task']; busy: boolean; onAction: (kind: 'cancel' | 'retry') => void }) {
  const canRetry = (task.status === 'failed' || task.status === 'cancelled') && task.attempts.length < task.maxAttempts
  return <div className="flex gap-2">{active(task.status) && <Button size="sm" variant="outline" disabled={busy || task.cancelRequested} onClick={() => onAction('cancel')}>{task.cancelRequested ? 'Cancellation requested' : 'Cancel report'}</Button>}{canRetry && <Button size="sm" variant="outline" disabled={busy} onClick={() => onAction('retry')}>Retry report</Button>}</div>
}

function ReportDocument({ report, view }: { report: DistributionReportDocument; view: DatasetViewDefinition }) {
  const coverage = report.sections.find((section): section is Extract<DistributionReportSection, { kind: 'coverage_schema' }> => section.kind === 'coverage_schema')
  const sections = report.sections.filter((section) => section.kind !== 'coverage_schema')
  const sampling = view.sampling.kind === 'all' ? 'All matching rows' : `Reservoir sample of ${count(view.sampling.size)} rows · seed ${view.sampling.seed}`
  return <><Evidence report={report} view={view} coverage={coverage} sampling={sampling} />
    <div className="grid gap-3">{sections.map((section) => <Section key={section.sectionId} section={section} measuredRows={report.measuredRows} />)}</div></>
}

function Evidence({ report, view, coverage, sampling }: { report: DistributionReportDocument; view: DatasetViewDefinition; coverage: Extract<DistributionReportSection, { kind: 'coverage_schema' }> | undefined; sampling: string }) {
  return <section className="grid gap-2 rounded-lg border border-border bg-muted/20 p-4 text-[11px]"><div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Coverage before distributions</div>
    <div className="grid gap-1 sm:grid-cols-2"><div><strong>Dataset / revision:</strong> <span className="font-mono break-all">{report.datasetId}@{report.revisionId}</span></div><div><strong>View:</strong> {view.name} · <span className="font-mono">{report.viewDefinitionSha256}</span></div><div><strong>Population:</strong> {sampling}</div><div><strong>Measured:</strong> {count(report.measuredRows)} rows · {report.complete ? 'complete for this view' : 'sample only; no full-population claim'}</div><div><strong>Columns:</strong> {coverage ? `${coverage.reportedColumnCount} of ${coverage.selectedColumnCount} selected` : 'coverage document unavailable'}</div><div><strong>Computation:</strong> {report.computationVersion}</div></div>
    {report.sampleProvenance && <div className="rounded border border-border bg-background p-2"><strong>Sample evidence:</strong> {count(report.sampleProvenance.returnedRows)} returned{report.sampleProvenance.totalRows != null ? ` of ${count(report.sampleProvenance.totalRows)}` : ' of unknown total'} · scanned {report.sampleProvenance.scannedRows == null ? 'unknown' : count(report.sampleProvenance.scannedRows)} · {report.sampleProvenance.strategy}{report.sampleProvenance.seed != null ? ` · seed ${report.sampleProvenance.seed}` : ''}</div>}
    {report.limitations.length > 0 && <ul className="list-disc pl-4 text-muted-foreground">{report.limitations.map((item) => <li key={item}>{item}</li>)}</ul>}
  </section>
}

function Section({ section, measuredRows }: { section: Exclude<DistributionReportSection, { kind: 'coverage_schema' }>; measuredRows: number }) {
  if (section.kind === 'missingness') return <section className="rounded-lg border border-border p-3 text-[11px]"><strong>{section.columnName}</strong><span className="ml-2 text-muted-foreground">Missingness</span><div className="mt-2"><Bar value={section.missingCount} total={measuredRows} /><span>{count(section.missingCount)} null{section.missingCount === 1 ? '' : 's'} of {count(measuredRows)} measured rows</span></div></section>
  if (section.kind === 'unsupported') return <section className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-[11px]"><strong>{section.columnName ?? 'Additional columns'}</strong><span className="ml-2 text-amber-800">Unsupported / partial: {section.reason.replaceAll('_', ' ')}</span>{section.omittedCount != null && <div className="mt-1">{count(section.omittedCount)} columns omitted by the report bound.</div>}</section>
  if (section.kind === 'numeric') return <section className="grid gap-2 rounded-lg border border-border p-3 text-[11px]"><strong>{section.columnName}</strong><div className="grid gap-1 text-muted-foreground sm:grid-cols-4"><span>Finite {count(section.count)}</span><span>Non-finite {count(section.nonFiniteCount)}</span><span>Min / max {number(section.min)} / {number(section.max)}</span><span>Mean / sd {number(section.mean)} / {number(section.stddev)}</span></div><Bars items={section.histogram.map((bucket) => ({ key: bucket.bucketId, label: `${number(bucket.lower)}–${number(bucket.upper)}${bucket.upperInclusive ? ']' : ')'}`, count: bucket.count }))} /><div className="text-muted-foreground">Quantiles: {section.quantiles.map((item) => `p${Math.round(item.probability * 100)} ${number(item.value)}`).join(' · ')}</div></section>
  if (section.kind === 'categorical') return <section className="grid gap-2 rounded-lg border border-border p-3 text-[11px]"><strong>{section.columnName}</strong><span className="text-muted-foreground">{section.distinctCountApproximate ? 'Approximate' : 'Exact'} distinct count: {count(section.distinctCount)}</span><Bars items={[...section.top.map((item) => ({ key: item.bucketId, label: `Value: ${String(item.label)}`, count: item.count })), { key: `${section.sectionId}:remainder`, label: 'Other (top-K remainder)', count: section.otherCount }]} /></section>
  return <section className="grid gap-2 rounded-lg border border-border p-3 text-[11px]"><strong>{section.columnName}</strong><span className="text-muted-foreground">UTC range: {utcDate(section.min)} – {utcDate(section.max)}</span><Bars items={section.buckets.map((bucket) => ({ key: bucket.bucketId, label: `${utcDate(bucket.start)} – ${utcDate(bucket.end)}${bucket.endInclusive ? ' inclusive' : ''}`, count: bucket.count }))} /></section>
}

function Bars({ items }: { items: Array<{ key?: string; label: string; count: number }> }) {
  const largest = Math.max(1, ...items.map((item) => item.count))
  return <div className="grid gap-1">{items.map((item) => <div key={item.key ?? item.label} className="grid grid-cols-[minmax(90px,1fr)_minmax(80px,2fr)_auto] items-center gap-2"><span className="truncate" title={item.label}>{item.label}</span><Bar value={item.count} total={largest} /><span className="font-mono text-muted-foreground">{count(item.count)}</span></div>)}</div>
}

function Bar({ value, total }: { value: number; total: number }) {
  return <span className="h-2 overflow-hidden rounded bg-muted" aria-label={`${count(value)} of ${count(total)} rows`}><span className={`block h-full rounded bg-primary${value > 0 ? ' min-w-px' : ''}`} style={{ width: `${Math.min(100, total ? value / total * 100 : 0)}%` }} /></span>
}

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
