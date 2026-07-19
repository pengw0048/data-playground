import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DatasetViewDefinition, DistributionReportEnvelope } from '../types/api'

const mocks = vi.hoisted(() => ({ distributionReport: vi.fn(), distributionReports: vi.fn(), estimateDistributionReport: vi.fn(), submitDistributionReport: vi.fn(), cancelRun: vi.fn(), retryRun: vi.fn() }))
vi.mock('../api/client', () => ({
  api: mocks,
  KernelError: class KernelError extends Error {
    status: number
    code?: string
    retryable?: boolean
    constructor(status: number, message: string, code?: string, retryable?: boolean) {
      super(message); this.status = status; this.code = code; this.retryable = retryable
    }
  },
}))

import { KernelError } from '../api/client'
import { DistributionReportLauncher, DistributionReportPage } from './DistributionReports'

const view: DatasetViewDefinition = {
  schemaVersion: 1, id: 'view-1', creatorId: 'local', name: 'Exact observations',
  datasetRef: { kind: 'exact', datasetId: 'dataset-1', revisionId: 'revision-1' },
  placement: { containerId: 'root', placementId: 'placement-1', sourceRegistrationId: 'registration-1' },
  selectedColumns: ['number', 'category', 'when', 'blob'], predicate: null, sampling: { kind: 'reservoir', size: 10, seed: 7 },
  sampleProvenance: { strategy: 'reservoir', seed: 7, requestedRows: 10, scannedRows: 100, returnedRows: 10, totalRows: 100, identity: 'sample-7', limitations: ['Deterministic sample.'] },
  retentionOwner: 'core', createdAt: '2026-07-18T00:00:00Z', semanticSha256: 'a'.repeat(64), definitionSha256: 'b'.repeat(64),
}

const envelope = (reportId = 'a'.repeat(32)): DistributionReportEnvelope => ({
  schemaVersion: 1, reportId, createdAt: '2026-07-18T00:00:00Z', updatedAt: '2026-07-18T00:01:00Z', completedAt: '2026-07-18T00:01:00Z',
  intent: { submissionId: 'submission-1', datasetViewId: view.id, viewDefinitionSha256: view.definitionSha256, computationVersion: 'distribution-v1', maxAttempts: 3 },
  viewSnapshot: view, revisionRetentionOwner: 'core',
  task: { id: 'task-1', status: 'done', progress: 1, error: null, cancelRequested: false, maxAttempts: 3, attempts: [{ id: 'attempt-1', attemptNumber: 1, status: 'done', progress: 1 }] },
  report: { schemaVersion: 1, reportId, taskId: 'task-1', datasetViewId: view.id, datasetId: 'dataset-1', revisionId: 'revision-1', viewDefinitionSha256: view.definitionSha256, computationVersion: 'distribution-v1', measuredRows: 10, complete: false, sampleProvenance: view.sampleProvenance, limitations: ['Counts are exact only for the deterministic sample.'], sections: [
    { kind: 'coverage_schema', sectionId: 'coverage', selectedColumnCount: 4, reportedColumnCount: 3, columns: [] },
    { kind: 'missingness', sectionId: 'missing-number', columnName: 'number', missingCount: 1 },
    { kind: 'numeric', sectionId: 'numeric', columnName: 'number', count: 8, nonFiniteCount: 1, min: 1e-9, max: 8, mean: 4, stddev: 2, quantiles: [{ probability: 0, value: 1e-9 }, { probability: .25, value: 2 }, { probability: .5, value: 4 }, { probability: .75, value: 6 }, { probability: 1, value: 8 }], histogram: [{ bucketId: 'bucket', lower: 1e-9, upper: 8, count: 8, upperInclusive: true }] },
    { kind: 'categorical', sectionId: 'category', columnName: 'category', top: [{ bucketId: 'top', label: 'Other', count: 7 }], otherCount: 2, distinctCount: 42, distinctCountApproximate: true },
    { kind: 'temporal', sectionId: 'when', columnName: 'when', min: '2026-01-01T00:00:00Z', max: '2026-01-02T00:00:00Z', buckets: [{ bucketId: 'time', start: '2026-01-01T00:00:00Z', end: '2026-01-02T00:00:00Z', count: 9, endInclusive: true }] },
    { kind: 'unsupported', sectionId: 'blob', columnName: 'blob', reason: 'unsupported_type', partial: true },
  ] },
})

describe('DistributionReportPage', () => {
  beforeEach(() => { vi.clearAllMocks(); mocks.distributionReport.mockResolvedValue(envelope()) })

  it('puts exact coverage and sample evidence before bounded renderers', async () => {
    const { container } = render(<DistributionReportPage reportId={'a'.repeat(32)} />)
    expect(await screen.findByText('Coverage before distributions')).toBeVisible()
    const text = container.textContent ?? ''
    expect(text.indexOf('Coverage before distributions')).toBeLessThan(text.indexOf('Missingness'))
    expect(screen.getByText(/sample only; no full-population claim/)).toBeVisible()
    expect(screen.getByText(/Approximate distinct count: 42/)).toBeVisible()
    expect(screen.getByText(/Unsupported \/ partial: unsupported type/)).toBeVisible()
    expect(screen.getByText(/Non-finite 1/)).toBeVisible()
    expect(screen.getByText(/scanned 100/)).toBeVisible()
    expect(screen.getByText(/Min \/ max 1e-9 \/ 8/)).toBeVisible()
    expect(screen.getByText('Value: Other')).toBeVisible()
    expect(screen.getByText('Other (top-K remainder)')).toBeVisible()
  })

  it('uses shortest round-trip text for small and high-magnitude finite numbers', async () => {
    const exact = envelope()
    mocks.distributionReport.mockResolvedValue({
      ...exact,
      report: {
        ...exact.report!,
        sections: exact.report!.sections.map((section) => section.kind === 'numeric' ? {
          ...section,
          min: 1.2345678901234568e-7,
          max: 1_000_000_000_000_001,
        } : section),
      },
    })
    render(<DistributionReportPage reportId={'a'.repeat(32)} />)
    expect(await screen.findByText(
      'Min / max 1.2345678901234568e-7 / 1000000000000001',
    )).toBeVisible()
  })

  it('keeps adjacent histogram boundaries distinct', async () => {
    const exact = envelope()
    mocks.distributionReport.mockResolvedValue({
      ...exact,
      report: {
        ...exact.report!,
        sections: exact.report!.sections.map((section) => section.kind === 'numeric' ? {
          ...section,
          histogram: [{
            bucketId: 'adjacent',
            lower: 1.000000000000001,
            upper: 1.000000000000002,
            count: 8,
            upperInclusive: true,
          }],
        } : section),
      },
    })
    render(<DistributionReportPage reportId={'a'.repeat(32)} />)
    expect(await screen.findByText('1.000000000000001–1.000000000000002]')).toBeVisible()
  })

  it('renders zero missingness, histogram, and remainder bars at exactly zero width', async () => {
    const exact = envelope()
    mocks.distributionReport.mockResolvedValue({
      ...exact,
      report: {
        ...exact.report!,
        sections: exact.report!.sections.map((section) => {
          if (section.kind === 'missingness') return { ...section, missingCount: 0 }
          if (section.kind === 'numeric') return { ...section, histogram: [{ ...section.histogram[0], count: 0 }] }
          if (section.kind === 'categorical') return { ...section, otherCount: 0 }
          return section
        }),
      },
    })
    render(<DistributionReportPage reportId={'a'.repeat(32)} />)
    await screen.findByText('Coverage before distributions')

    for (const label of ['0 of 10 rows', '0 of 1 rows', '0 of 7 rows']) {
      const fill = screen.getByLabelText(label).firstElementChild
      expect(fill).toHaveStyle({ width: '0%' })
      expect(fill).not.toHaveClass('min-w-px')
    }
  })

  it('renders temporal evidence in UTC instead of the browser timezone', async () => {
    const local = vi.spyOn(Date.prototype, 'toLocaleString').mockReturnValue('LOCAL TIME')
    render(<DistributionReportPage reportId={'a'.repeat(32)} />)
    const temporal = (await screen.findByText('when')).closest('section')
    expect(temporal).not.toBeNull()
    expect(within(temporal!).getByText(/UTC range:/)).not.toHaveTextContent('LOCAL TIME')
    expect(temporal).toHaveTextContent('UTC')
    local.mockRestore()
  })

  it('does not attach a stale report response after the exact report changes', async () => {
    let oldResolve!: (value: DistributionReportEnvelope) => void
    let newResolve!: (value: DistributionReportEnvelope) => void
    mocks.distributionReport.mockImplementation((id: string) => new Promise((resolve) => {
      if (id === 'old') oldResolve = resolve; else newResolve = resolve
    }))
    const page = render(<DistributionReportPage reportId="old" />)
    await waitFor(() => expect(mocks.distributionReport).toHaveBeenCalledWith('old'))
    page.rerender(<DistributionReportPage reportId="new" />)
    expect(screen.getByText('Loading exact retained report…')).toBeVisible()
    expect(screen.queryByText(/currently unavailable/)).not.toBeInTheDocument()
    const latest = { ...envelope('c'.repeat(32)), viewSnapshot: { ...view, name: 'New exact report' } }
    const stale = { ...envelope('d'.repeat(32)), viewSnapshot: { ...view, name: 'Stale exact report' } }
    newResolve(latest)
    await screen.findByText('New exact report')
    oldResolve(stale)
    await waitFor(() => expect(mocks.distributionReport).toHaveBeenCalledWith('new'))
    expect(screen.getByText('New exact report')).toBeVisible()
    expect(screen.queryByText('Stale exact report')).not.toBeInTheDocument()
    expect(screen.queryByText('Loading exact retained report…')).not.toBeInTheDocument()
  })

  it.each([
    { action: 'cancel' as const, button: 'Cancel report', status: 'running' as const },
    { action: 'retry' as const, button: 'Retry report', status: 'failed' as const },
  ])('does not let a completed $action action reload its old report identity', async ({ action, button, status }) => {
    let finishAction!: () => void
    const old = { ...envelope('a'.repeat(32)), viewSnapshot: { ...view, name: 'Old action report' }, report: null,
      task: { ...envelope().task, id: 'old-task', status, progress: null, attempts: [{ id: 'old-attempt', attemptNumber: 1, status, progress: null }] } }
    const latest = { ...envelope('b'.repeat(32)), viewSnapshot: { ...view, name: 'New action report' } }
    mocks.distributionReport.mockImplementation((id: string) => Promise.resolve(id === 'old' ? old : latest))
    const actionMock = action === 'cancel' ? mocks.cancelRun : mocks.retryRun
    actionMock.mockReturnValue(new Promise<void>((resolve) => { finishAction = resolve }))
    const page = render(<DistributionReportPage reportId="old" />)
    await screen.findByText('Old action report')
    fireEvent.click(screen.getByRole('button', { name: button }))
    await waitFor(() => expect(actionMock).toHaveBeenCalled())
    page.rerender(<DistributionReportPage reportId="new" />)
    await screen.findByText('New action report')
    await act(async () => { finishAction() })
    expect(screen.getByText('New action report')).toBeVisible()
    expect(screen.queryByText('Old action report')).not.toBeInTheDocument()
    expect(mocks.distributionReport.mock.calls.filter(([id]) => id === 'old')).toHaveLength(1)
  })

  it('restarts polling when a running report switches to another running report', async () => {
    const running = (name: string, taskId: string) => ({
      ...envelope(), viewSnapshot: { ...view, name }, report: null,
      task: { ...envelope().task, id: taskId, status: 'running' as const, progress: .5,
        attempts: [{ id: `${taskId}-attempt`, attemptNumber: 1, status: 'running' as const, progress: .5 }] },
    })
    const old = running('Old running report', 'old-task')
    const latest = running('New running report', 'new-task')
    mocks.distributionReport.mockImplementation((id: string) => Promise.resolve(id === 'old' ? old : latest))
    const page = render(<DistributionReportPage reportId="old" />)
    await screen.findByText('Old running report')
    page.rerender(<DistributionReportPage reportId="new" />)
    await screen.findByText('New running report')

    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1_100)) })

    expect(mocks.distributionReport.mock.calls.filter(([id]) => id === 'old')).toHaveLength(1)
    expect(mocks.distributionReport.mock.calls.filter(([id]) => id === 'new').length).toBeGreaterThanOrEqual(2)
  })

  it.each([
    { status: 410, code: 'resource_gone', expected: 'exact retained revision required by this report is no longer available' },
    { status: 500, code: 'internal_error', expected: 'report state could not be validated because it is corrupt' },
    { status: 404, code: undefined, expected: 'does not exist, was deleted, or is not visible to you' },
    { status: 403, code: 'permission_denied', expected: 'not authorized to open this retained report' },
  ])('classifies a $status report load from stable API status and code', async ({ status, code, expected }) => {
    mocks.distributionReport.mockRejectedValue(new KernelError(status, 'sanitized API detail', code, false))
    render(<DistributionReportPage reportId="unavailable" />)
    expect(await screen.findByRole('alert')).toHaveTextContent(expected)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible()
  })

  it('explains an active report whose exact revision became unavailable', async () => {
    mocks.distributionReport.mockResolvedValue({
      ...envelope(), report: null,
      task: { ...envelope().task, status: 'failed', error: 'distribution report revision unavailable',
        progress: null, attempts: [{ id: 'failed-attempt', attemptNumber: 1, status: 'failed', progress: null }] },
    })
    render(<DistributionReportPage reportId="revision-gone" />)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The exact retained revision became unavailable before this report finished.',
    )
  })
})

describe('DistributionReportLauncher', () => {
  beforeEach(() => { vi.clearAllMocks(); mocks.distributionReports.mockResolvedValue([]) })

  it('requires an explicit confirmation when retained metadata cannot prove scan size', async () => {
    mocks.estimateDistributionReport.mockResolvedValue({ schemaVersion: 1, datasetViewId: view.id, viewDefinitionSha256: view.definitionSha256, estimatedScanRows: null, estimatedScanBytes: null, selectedColumnCount: 4, needsConfirmation: true, reason: 'unknown_size', limits: { reportedColumns: 64, topCategories: 20, histogramBuckets: 20, deadlineSeconds: 30 } })
    mocks.submitDistributionReport.mockResolvedValue(envelope())
    render(<DistributionReportLauncher definition={view} />)
    await screen.findByText('No retained reports yet.')
    fireEvent.click(screen.getByRole('button', { name: 'Inspect distributions' }))
    expect(await screen.findByRole('dialog', { name: 'Confirm distribution report' })).toHaveTextContent('does not prove the scan size')
    expect(mocks.submitDistributionReport).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and start' }))
    await waitFor(() => expect(mocks.submitDistributionReport).toHaveBeenCalledWith(view.id, expect.any(String), true))
  })

  it('uses the stable API code when the exact revision disappears before submission', async () => {
    mocks.estimateDistributionReport.mockResolvedValue({ schemaVersion: 1, datasetViewId: view.id, viewDefinitionSha256: view.definitionSha256, estimatedScanRows: 10, estimatedScanBytes: 100, selectedColumnCount: 4, needsConfirmation: false, reason: null, limits: { reportedColumns: 64, topCategories: 20, histogramBuckets: 20, deadlineSeconds: 30 } })
    mocks.submitDistributionReport.mockRejectedValue(new KernelError(
      410, 'sanitized API detail', 'resource_gone', false,
    ))
    render(<DistributionReportLauncher definition={view} />)
    await screen.findByText('No retained reports yet.')
    fireEvent.click(screen.getByRole('button', { name: 'Inspect distributions' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The exact retained revision is no longer available; no new report was started.',
    )
  })
})
