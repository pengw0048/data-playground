import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { fmtMs, DurationTrend, PerNodeBreakdown } from './RunHistoryModal'
import type { PerNodeStat, RunRecordDto } from '../api/client'
import { RunHistoryModal } from './RunHistoryModal'
import { DataPanel, FullResult } from './DataPanel'
import { previewPlanIdentity, profilePlanIdentity, useStore } from '../store/graph'
import { register } from '../nodes/registry'

const apiMock = vi.hoisted(() => ({
  listRuns: vi.fn(),
  executionManifest: vi.fn(),
  tableByRegistration: vi.fn(),
  datasetRevision: vi.fn(),
  sample: vi.fn(),
  runOutputSample: vi.fn(),
  preview: vi.fn(),
  profile: vi.fn(),
  profileEstimate: vi.fn(),
  fullProfile: vi.fn(),
  cancelRun: vi.fn(),
  fullResultExportUrl: vi.fn(),
  preflightFullResultExport: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: apiMock }))

afterEach(() => {
  document.querySelectorAll('iframe[data-full-result-download]').forEach((frame) => frame.remove())
  vi.restoreAllMocks()
})

function registerAssertUiTestNode() {
  register({
    kind: 'assert-ui-test', title: 'assert', category: 'compute', inputs: [],
    outputs: [{ id: 'pass', label: 'Passing', wire: 'dataset' }, { id: 'out', label: 'Violations', wire: 'dataset' }],
    canBypass: false, blurb: '',
    defaultData: () => ({ title: 'assert', status: 'draft', history: [], config: {} }),
  }, () => null)
}

beforeEach(() => {
  apiMock.listRuns.mockReset()
  apiMock.executionManifest.mockReset()
  apiMock.tableByRegistration.mockReset()
  apiMock.datasetRevision.mockReset()
  apiMock.sample.mockReset()
  apiMock.runOutputSample.mockReset()
  apiMock.preview.mockReset()
  apiMock.profile.mockReset().mockResolvedValue({
    columns: [], rowCount: 10, sampled: true, completeness: 'sample',
  })
  apiMock.profileEstimate.mockReset().mockResolvedValue({
    rows: null, bytes: null, placement: 'local', needsConfirm: true,
    targetPortId: 'out', planDigest: 'a'.repeat(64),
  })
  apiMock.fullProfile.mockReset().mockResolvedValue({
    runId: 'profile-ui', status: 'done', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
    planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
    rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    profile: { columns: [], rowCount: 10, sampled: false, completeness: 'complete' },
  })
  apiMock.cancelRun.mockReset().mockImplementation(async (runId: string) => ({
    runId, status: 'cancelled', jobType: 'run',
    rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
  }))
  apiMock.fullResultExportUrl.mockReset().mockReturnValue('/api/run/full-result-export')
  apiMock.preflightFullResultExport.mockReset().mockResolvedValue('/api/run/full-result-export')
  useStore.setState({
    currentUser: { id: 'alice', name: 'Alice' },
    doc: { id: 'history-canvas', name: 'History', version: 1, nodes: [], edges: [], requirements: [] },
    previews: {}, runs: {}, toasts: [],
  } as any)
})

describe('fmtMs — human-readable durations', () => {
  it('scales ms → s → m across thresholds', () => {
    expect(fmtMs(0)).toBe('0 ms')
    expect(fmtMs(950)).toBe('950 ms')
    expect(fmtMs(1500)).toBe('1.5 s')
    expect(fmtMs(42_000)).toBe('42 s')
    expect(fmtMs(125_000)).toBe('2m 5s')
  })
  it('carries across unit boundaries instead of showing 60s / Xm 60s', () => {
    expect(fmtMs(9_999)).toBe('10 s')      // not "10.0 s"
    expect(fmtMs(59_999)).toBe('1m 0s')    // not "60 s"
    expect(fmtMs(119_500)).toBe('2m 0s')   // not "1m 60s"
    expect(fmtMs(60_000)).toBe('1m 0s')
  })
})

describe('DurationTrend — a native SVG bar per run', () => {
  const runs: RunRecordDto[] = [
    { id: 'r2', status: 'failed', ms: 200, outputs: [] },
    { id: 'r1', status: 'done', ms: 100, outputs: [] },
  ]
  it('renders one rect per run and reports the max duration', () => {
    const { container } = render(<DurationTrend runs={runs} />)
    expect(container.querySelectorAll('rect')).toHaveLength(2)
    expect(screen.getByText('max 200 ms')).toBeInTheDocument()
    expect(screen.getByText('Run duration · last 2')).toBeInTheDocument()
  })
})

describe('PerNodeBreakdown — per-node horizontal bars', () => {
  const nodes: PerNodeStat[] = [
    { nodeId: 'src', label: 'source', status: 'done', ms: 10, rows: 5 },
    { nodeId: 'wr', label: 'write', status: 'done', ms: 90, rows: 5 },
  ]
  it('lists every node with its duration', () => {
    render(<PerNodeBreakdown nodes={nodes} />)
    expect(screen.getByText('source')).toBeInTheDocument()
    expect(screen.getByText('write')).toBeInTheDocument()
    expect(screen.getByText('Plan build time per node')).toBeInTheDocument()
    expect(screen.getByText('90 ms')).toBeInTheDocument()
  })
})

describe('Run history Jobs identity', () => {
  it('does not substitute a history-row id for a missing durable Job identity', async () => {
    apiMock.listRuns.mockResolvedValue([
      { id: 'history-row-only', status: 'done', outputs: [] },
      { id: 'history-row', runId: 'authorized-run', status: 'failed', outputs: [] },
    ])
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    const links = await screen.findAllByRole('button', { name: 'View in Jobs' })
    expect(links).toHaveLength(1)
    await user.click(links[0])
    expect(useStore.getState().jobsQuery).toBe('run=authorized-run')
  })
})

describe('admitted run inputs', () => {
  const manifestItem = (nodeId: string, datasetId: string, revisionId: string) => ({
    node_id: nodeId, dataset_id: datasetId, revision_id: revisionId,
    provider: 'lance', resolved_at: '2026-07-16T12:00:00Z',
  })
  const revisionDetail = (datasetId: string, revisionId: string) => ({
    datasetId, revisionId, committedAt: '2026-07-16T11:00:00Z', retentionOwner: 'provider' as const,
    parentRevisionId: 'parent-1', producerOperation: null,
    summary: { rowCount: 12, dataFileCount: 1, totalBytes: 100, fragmentCount: 1 },
    preview: { columns: [], rows: [{ value: 1 }], hasMore: false, rowLimit: 100 as const },
  })

  it('preserves manifest order and opens the admitted exact revision through Catalog', async () => {
    useStore.setState({ doc: {
      id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [
        { id: 'source-b', type: 'source', position: { x: 0, y: 0 }, data: { title: 'Customers', status: 'latest', config: {}, history: [] } },
        { id: 'source-a', type: 'source', position: { x: 0, y: 0 }, data: { title: 'Orders', status: 'latest', config: {}, history: [] } },
      ],
    } } as any)
    apiMock.listRuns.mockResolvedValue([{
      id: 'manifest-history', runId: 'manifest-run', jobType: 'run', status: 'done', outputs: [],
      inputManifest: [manifestItem('source-a', 'dataset-a', 'revision-a'), manifestItem('source-b', 'dataset-b', 'revision-b')],
    }])
    apiMock.tableByRegistration.mockImplementation(async (datasetId: string) => ({
      id: datasetId, name: datasetId === 'dataset-a' ? 'Orders dataset' : 'Customers dataset',
      uri: `file:///${datasetId}.parquet`, columns: [],
    }))
    apiMock.datasetRevision.mockImplementation(async (datasetId: string, revisionId: string) => revisionDetail(datasetId, revisionId))
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    await user.click(await screen.findByRole('button', { name: /Admitted inputs/i }))
    const list = screen.getByRole('list')
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(within(items[0]).getByText('Source Orders')).toBeInTheDocument()
    expect(within(items[1]).getByText('Source Customers')).toBeInTheDocument()
    expect(await within(items[0]).findByText('Orders dataset')).toBeInTheDocument()
    expect(within(items[0]).getByText('Exact revision revision-a')).toBeInTheDocument()
    expect(within(items[0]).getByText(/Reference intent was not stored/)).toBeInTheDocument()
    await user.click(within(items[0]).getByRole('button', { name: 'Open Catalog revision detail' }))
    expect(within(items[0]).getByText('12')).toBeInTheDocument()
    expect(apiMock.datasetRevision).toHaveBeenCalledWith('dataset-a', 'revision-a')
  })

  it('reports unavailable, permission-lost, and provider-offline revisions without substituting latest', async () => {
    apiMock.listRuns.mockResolvedValue([{
      id: 'unavailable-history', runId: 'unavailable-run', jobType: 'run', status: 'failed', outputs: [],
      inputManifest: [
        manifestItem('missing', 'dataset-missing', 'revision-missing'),
        manifestItem('denied', 'dataset-denied', 'revision-denied'),
        manifestItem('offline', 'dataset-offline', 'revision-offline'),
      ],
    }])
    apiMock.tableByRegistration.mockRejectedValue(Object.assign(new Error('registration unavailable'), { status: 404 }))
    apiMock.datasetRevision.mockImplementation(async (datasetId: string) => {
      const status = datasetId === 'dataset-missing' ? 410 : datasetId === 'dataset-denied' ? 403 : 503
      throw Object.assign(new Error('unavailable'), { status })
    })
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    await user.click(await screen.findByRole('button', { name: /Admitted inputs/i }))
    expect(await screen.findByText('permission lost')).toBeInTheDocument()
    expect(screen.getByText('provider offline')).toBeInTheDocument()
    expect(screen.getByText(/missing or compacted.*Latest was not substituted/i)).toBeInTheDocument()
    expect(screen.getByText(/provider is offline or unavailable/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open Catalog revision detail' })).not.toBeInTheDocument()
  })

  it('labels a pre-manifest run as legacy evidence', async () => {
    apiMock.listRuns.mockResolvedValue([{
      id: 'legacy-history', runId: 'legacy-run', jobType: 'run', status: 'done', outputs: [], inputManifest: null,
    }])
    render(<RunHistoryModal onClose={() => {}} />)

    expect(await screen.findByText('No admitted input manifest was recorded for this legacy run.')).toBeInTheDocument()
    expect(apiMock.datasetRevision).not.toHaveBeenCalled()
  })
})

describe('execution manifest inspection', () => {
  it('loads the immutable detail lazily and renders every recorded contract section', async () => {
    const digest = 'a'.repeat(64)
    apiMock.listRuns.mockResolvedValue([{
      id: 'manifest-history', runId: 'manifest-run', jobType: 'run', status: 'failed',
      outputs: [], executionManifestSha256: digest, executionManifestSchemaVersion: 1,
      executionManifestAvailability: 'available', executionManifestReconstructable: true,
    }])
    apiMock.executionManifest.mockResolvedValue({
      sha256: digest, schemaVersion: 1, availability: 'available', document: {
        schemaVersion: 1,
        graph: {
          nodes: [{ id: 'source', type: 'source', data: { config: {} } }],
          edges: [], requirements: ['polars==1.0'],
        },
        target: { nodeId: 'source', portId: null },
        admittedInputs: [{ nodeId: 'source', datasetId: 'dataset-1', revisionId: 'revision-1', provider: 'local' }],
        writeIntent: { mode: 'create', destination: { name: 'result' } },
        descriptors: { core: { apiVersion: '1' }, nodes: [], plugins: [] },
      },
    })
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    const toggle = await screen.findByRole('button', { name: /Execution manifest/ })
    expect(apiMock.executionManifest).not.toHaveBeenCalled()
    await user.click(toggle)

    expect(await screen.findByText('Submitted graph')).toBeVisible()
    expect(screen.getByText('Admitted write intent')).toBeVisible()
    expect(screen.getByText('Runtime descriptor snapshot')).toBeVisible()
    expect(screen.getByText('No declared parameter bindings were recorded.')).toBeVisible()
    expect(screen.getByText(/dataset-1@revision-1/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Clone as new Canvas…' })).toBeVisible()
    expect(apiMock.executionManifest).toHaveBeenCalledWith('history-canvas', 'manifest-history')
  })
})

describe('durable full results', () => {
  const committedOutput = (uri: string, rows: number, portId = 'out') => ({
    nodeId: 'target', portId, wire: 'dataset' as const, publicationKind: 'result' as const,
    outcome: 'committed' as const, uri, rows,
  })
  const sample = (offset: number, count: number, hasMore: boolean) => ({
    columns: [{ name: 'v', type: 'BIGINT', capabilities: [] }],
    rows: Array.from({ length: count }, (_, i) => ({ v: offset + i })),
    rowCount: 105,
    hasMore,
    truncated: hasMore,
    completeness: hasMore || offset > 0 ? 'page' as const : 'complete' as const,
  })
  const fullIdentity = {
    runId: 'run-direct', nodeId: 'target', portId: 'out', publicationKind: 'result' as const,
  }

  it('reopens a completed result from persisted run history', async () => {
    apiMock.listRuns.mockResolvedValue([{ id: 'history-row', runId: 'run-real', status: 'done',
      targetNodeId: 'target', rows: 105, outputs: [committedOutput('/outputs/result.parquet', 105)] }])
    apiMock.runOutputSample.mockResolvedValue(sample(0, 50, true))
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    await user.click(await screen.findByRole('button', { name: 'Open full result' }))
    expect(await screen.findByText(/rows 1–50 of 105/)).toBeInTheDocument()
    expect(apiMock.runOutputSample).toHaveBeenCalledWith('run-real', 'target', 'out', 50, 0)
    await user.click(screen.getByRole('button', { name: 'Export full result' }))
    await waitFor(() => expect(apiMock.preflightFullResultExport).toHaveBeenCalledWith(
      'run-real', 'target', 'out', 'target-out',
    ))
    expect(apiMock.fullResultExportUrl).not.toHaveBeenCalled()
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe('/api/run/full-result-export')
  })

  it('shows every named history output and keeps a committed artifact inspectable after overall failure', async () => {
    apiMock.listRuns.mockResolvedValue([{
      id: 'partial-history', runId: 'partial-run', status: 'failed', targetNodeId: 'target', rows: null,
      error: 'out failed', outputs: [
        committedOutput('/outputs/pass.parquet', 7, 'pass'),
        {
          nodeId: 'target', portId: 'out', portLabel: 'Violations', wire: 'dataset',
          publicationKind: 'result', outcome: 'failed', error: 'writer failed',
        },
      ],
    }])
    apiMock.runOutputSample.mockResolvedValue({ ...sample(0, 7, false), rows: [{ v: 'survived' }] })
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    expect(await screen.findByLabelText('Outputs for run partial-history')).toBeInTheDocument()
    expect(screen.getByText('pass')).toBeInTheDocument()
    expect(screen.getByText('Violations')).toBeInTheDocument()
    expect(screen.getByText('writer failed')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open pass' }))
    expect(await screen.findByText('survived')).toBeInTheDocument()
  })

  it('shows persisted sample evidence beside a retained result', async () => {
    apiMock.listRuns.mockResolvedValue([{
      id: 'sample-history', runId: 'sample-run', status: 'done', targetNodeId: 'target', rows: 4,
      outputs: [{
        ...committedOutput('/outputs/sample.parquet', 4),
        sampleProvenance: {
          strategy: 'reservoir', seed: 7, requestedRows: 4, scannedRows: 12,
          returnedRows: 4, totalRows: 12, identity: 'a'.repeat(64), limitations: ['deterministic'],
        },
      }],
    }])

    render(<RunHistoryModal onClose={() => {}} />)

    expect(await screen.findByText(/Deterministic reservoir sample/)).toBeInTheDocument()
    expect(screen.getByText(/seed 7/)).toBeInTheDocument()
    expect(screen.getByText(/requested 4/)).toBeInTheDocument()
  })

  it('pages beyond the first 50 materialized rows', async () => {
    const downloads: string[] = []
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true, value: vi.fn(() => 'blob:full-result-page'),
    })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push(this.download)
    })
    apiMock.runOutputSample.mockImplementation(async (
      _runId: string, _nodeId: string, _portId: string, _k: number, offset: number,
    ) =>
      offset === 0 ? sample(0, 50, true) : sample(50, 50, true))
    const user = userEvent.setup()
    // History can hold rows written by one append attempt (50), while the reopened artifact is
    // cumulative (105). The artifact's measured rowCount is authoritative for display/paging.
    render(<FullResult uri="/outputs/result.parquet" total={50} {...fullIdentity} />)
    await screen.findByText(/rows 1–50 of 105/)

    await user.click(screen.getByRole('button', { name: 'Next page' }))
    expect(await screen.findByText(/rows 51–100 of 105/)).toBeInTheDocument()
    expect(apiMock.runOutputSample).toHaveBeenLastCalledWith('run-direct', 'target', 'out', 50, 50)
    await user.click(screen.getByRole('button', { name: 'Export this full-result page' }))
    await user.click(screen.getByRole('menuitem', { name: 'Download full-result page as CSV' }))
    expect(downloads).toContain('result-full-result-page-51-100.csv')
  })

  it('keeps unknown next-page availability explicit and exploratory', async () => {
    apiMock.runOutputSample.mockImplementation(async (
      _runId: string, _nodeId: string, _portId: string, _k: number, offset: number,
    ) => ({
      columns: [{ name: 'v', type: 'BIGINT', capabilities: [] }],
      rows: offset === 0 ? [{ v: 1 }] : [], rowCount: null, hasMore: null,
      truncated: true, completeness: 'unknown',
    }))
    const user = userEvent.setup()
    render(<FullResult uri="/outputs/unknown-pages.parquet" total={null} {...fullIdentity} />)

    expect(await screen.findByText('Next page availability unknown · You can try the next offset.')).toBeInTheDocument()
    const next = screen.getByRole('button', { name: 'Next page' })
    expect(next).toBeEnabled()
    await user.click(next)

    expect(await screen.findByText('Next page availability unknown.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeEnabled()
    expect(screen.getByText(/No rows at offset 1/)).toBeInTheDocument()
    expect(apiMock.runOutputSample).toHaveBeenLastCalledWith('run-direct', 'target', 'out', 50, 1)
    await user.click(screen.getByRole('button', { name: 'Previous page' }))
    expect(await screen.findByText(/rows 1–1/)).toBeInTheDocument()
    expect(apiMock.runOutputSample).toHaveBeenLastCalledWith('run-direct', 'target', 'out', 50, 0)
  })

  it('stops at the read budget while preserving truncated export labeling', async () => {
    apiMock.runOutputSample.mockImplementation(async (
      _runId: string, _nodeId: string, _portId: string, _k: number, offset: number,
    ) => ({
      ...sample(offset, 50, offset < 1950),
      rowCount: 100_000,
      truncated: true,
      completeness: offset === 1950 ? 'capped' : 'page',
      rowLimit: offset === 1950 ? 2_000 : undefined,
      limitReason: offset === 1950 ? 'interactive-row-budget' : undefined,
    }))
    const user = userEvent.setup()

    render(<FullResult uri="/outputs/budget-terminal.parquet" total={100_000} {...fullIdentity} />)

    await screen.findByText(/rows 1–50 of 100,000/)
    expect(screen.queryByText(/Interactive view stopped/)).not.toBeInTheDocument()
    for (let offset = 50; offset <= 1950; offset += 50) {
      await user.click(screen.getByRole('button', { name: 'Next page' }))
      await screen.findByText(new RegExp(
        `rows ${(offset + 1).toLocaleString()}–${(offset + 50).toLocaleString()} of 100,000`,
      ))
    }
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled()
    expect(screen.getByText(/Interactive view stopped at 2,000 rows of 100,000/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export this full-result page' })).toHaveTextContent('Export this page')
  })

  it('labels a missing or expired artifact explicitly', async () => {
    apiMock.runOutputSample.mockRejectedValue(new Error('404: no such file'))
    render(<FullResult uri="/outputs/missing.parquet" total={105} {...fullIdentity} />)
    expect(await screen.findByText('Full result expired or removed')).toBeInTheDocument()
    expect(screen.getByText(/stored artifact is no longer available/i)).toBeInTheDocument()
  })

  it('does not mislabel an authorization failure as expiration', async () => {
    apiMock.runOutputSample.mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }))
    render(<FullResult uri="/outputs/private.parquet" total={105} {...fullIdentity} />)
    expect(await screen.findByText('Full result access denied')).toBeInTheDocument()
    expect(screen.queryByText('Full result expired or removed')).not.toBeInTheDocument()
  })

  it.each([
    [{ error: true, reason: 'adapter failed while opening the artifact' }, 'Couldn’t read full result'],
    [{ notPreviewable: true, reason: 'this adapter has no bounded preview' }, 'Full result cannot be previewed'],
  ] as const)('renders a semantic sample failure instead of a fake empty result', async (response, title) => {
    apiMock.runOutputSample.mockResolvedValue({
      columns: [], rows: [], rowCount: null, hasMore: false, truncated: false,
      completeness: 'unknown', wire: 'dataset', notPreviewable: false, ...response,
    })

    render(<FullResult uri="/outputs/opaque.parquet" total={105} {...fullIdentity} />)

    expect(await screen.findByText(title)).toBeInTheDocument()
    expect(screen.getByText(response.reason)).toBeInTheDocument()
    expect(screen.queryByText(/Complete artifact/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export full result' })).toBeInTheDocument()
  })

  it('lets HEAD preflight decide exportability for a result URI without a file suffix', async () => {
    apiMock.runOutputSample.mockResolvedValue(sample(0, 1, false))

    render(<FullResult uri="s3://bucket/runs/attempt-123/" total={1} {...fullIdentity} />)

    expect(await screen.findByRole('button', { name: 'Export full result' })).toBeInTheDocument()
  })

  it('preflights a native export and reports rejection without navigating the SPA', async () => {
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click')
    apiMock.runOutputSample.mockResolvedValue(sample(0, 50, true))
    apiMock.preflightFullResultExport.mockRejectedValueOnce(new Error('artifact is not exportable'))
    const user = userEvent.setup()
    render(<FullResult uri="/outputs/result.parquet" total={105} {...fullIdentity} />)
    await screen.findByText(/rows 1–50 of 105/)

    await user.click(screen.getByRole('button', { name: 'Export full result' }))

    await waitFor(() => expect(useStore.getState().toasts.at(-1)).toMatchObject({
      kind: 'error', msg: 'Could not start full-result export: artifact is not exportable',
    }))
    expect(anchorClick).not.toHaveBeenCalled()
    expect(document.querySelector('iframe[data-full-result-download]')).toBeNull()
    expect(apiMock.fullResultExportUrl).not.toHaveBeenCalled()
  })

  it('does not substitute a history-row id when durable run identity is missing', async () => {
    apiMock.listRuns.mockResolvedValue([{ id: 'history-row-only', status: 'done',
      targetNodeId: 'target', rows: 105, outputs: [committedOutput('/outputs/result.parquet', 105)] }])
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    await user.click(await screen.findByRole('button', { name: 'Open full result' }))

    expect(await screen.findByText('Full result identity unavailable')).toBeInTheDocument()
    expect(screen.getByText(/has no durable run identity/i)).toBeInTheDocument()
    expect(apiMock.runOutputSample).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Export full result' })).not.toBeInTheDocument()
  })

  it('labels write counts without treating them as the catalog artifact total or export capability', async () => {
    const downloads: string[] = []
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true, value: vi.fn(() => 'blob:published-dataset-page'),
    })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push(this.download)
    })
    const catalogOutput = {
      ...committedOutput('/catalog/robot-actions.parquet', 12), publicationKind: 'catalog' as const,
      table: 'robot_actions',
    }
    apiMock.listRuns.mockResolvedValue([{ id: 'write-history', runId: 'write-run', status: 'done',
      targetNodeId: 'target', rows: 12, outputs: [catalogOutput] }])
    apiMock.runOutputSample.mockResolvedValue({
      columns: [{ name: 'v', type: 'BIGINT', capabilities: [] }], rows: [{ v: 1 }],
      rowCount: null, hasMore: true, truncated: true, completeness: 'page',
    })
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    expect(await screen.findByText('12 rows written')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open published dataset' }))

    expect(await screen.findByText(/Total rows unknown/)).toBeInTheDocument()
    expect(screen.queryByText(/of 12/)).not.toBeInTheDocument()
    expect(screen.getAllByText('Published dataset').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Export this published dataset page' })).toBeInTheDocument()
    expect(screen.queryByText('Full result artifact')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Export full result' })).not.toBeInTheDocument()
    expect(apiMock.runOutputSample).toHaveBeenCalledWith('write-run', 'target', 'out', 50, 0)
    await user.click(screen.getByRole('button', { name: 'Export this published dataset page' }))
    await user.click(screen.getByRole('menuitem', { name: 'Download published dataset page as CSV' }))
    expect(downloads).toContain('target-out-published-dataset-page-1-1.csv')
  })

  it('reloads the durable Lance append receipt from persisted run history', async () => {
    apiMock.listRuns.mockResolvedValue([{
      id: 'lance-write-history', runId: 'lance-write-run', status: 'done',
      targetNodeId: 'write', rows: 3, outputs: [{
        ...committedOutput('/outputs/existing.lance', 3), publicationKind: 'catalog' as const,
        table: 'existing', writeReceipt: {
          datasetId: 'dataset-lance', revisionId: '8', rows: 3, bytes: 1024,
          parentHead: { kind: 'exact', datasetId: 'dataset-lance', revisionId: '7' },
          publication: { backendVersion: '8.0.0' },
        },
      }],
    }])

    render(<RunHistoryModal onClose={() => {}} />)

    const receipt = await screen.findByLabelText('Write receipt for run lance-write-history')
    expect(receipt).toHaveTextContent(/durable revision 8/i)
    expect(receipt).toHaveTextContent(/dataset dataset-lance/i)
    expect(receipt).toHaveTextContent(/parent 7/i)
    expect(receipt).toHaveTextContent(/backend 8\.0\.0/i)
    expect(receipt).not.toHaveTextContent(/\/outputs\/existing\.lance/i)
  })

  it('offers sample/full switching for previewable nodes after a full run', async () => {
    apiMock.runOutputSample.mockResolvedValue(sample(0, 50, true))
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    useStore.setState({
      doc,
      previews: { target: boundPreview(doc, 'target', sample(0, 50, true)) },
      runs: { target: { phase: 'done', status: { runId: 'run-real', status: 'done',
        targetNodeId: 'target', rowsProcessed: 105, totalRows: 105, ms: 10, placement: 'local',
        perNode: [], outputs: [committedOutput('/outputs/result.parquet', 105)] } } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Full result' }))
    await waitFor(() => expect(apiMock.runOutputSample).toHaveBeenCalledWith('run-real', 'target', 'out', 50, 0))
    expect(screen.getByRole('button', { name: 'Preview sample' })).toBeInTheDocument()
  })

  it('keeps the Sample escape hatch when loading Full fails temporarily', async () => {
    apiMock.runOutputSample.mockRejectedValue(Object.assign(new Error('service unavailable'), { status: 503 }))
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    useStore.setState({
      doc,
      previews: { target: boundPreview(doc, 'target', sample(0, 50, true)) },
      runs: { target: { phase: 'done', status: { runId: 'run-real', status: 'done',
        targetNodeId: 'target', rowsProcessed: 105, totalRows: 105, ms: 10, placement: 'local',
        perNode: [], outputs: [committedOutput('/outputs/result.parquet', 105)] } } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)
    await user.click(screen.getByRole('button', { name: 'Full result' }))

    expect(await screen.findByText('Couldn’t load full result')).toBeInTheDocument()
    const sampleButton = screen.getByRole('button', { name: 'Preview sample' })
    expect(sampleButton).toBeInTheDocument()
    await user.click(sampleButton)
    expect(screen.queryByText('Couldn’t load full result')).not.toBeInTheDocument()
    expect(screen.getByText('rows 1–50')).toBeInTheDocument()
  })

  it('switches named output previews, sampled profiles, and full artifacts by the visible port tab', async () => {
    registerAssertUiTestNode()
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'assert-ui-test', position: { x: 0, y: 0 },
      data: { title: 'Quality gate', status: 'latest', config: {}, history: [] },
    }] }
    const passSample = { ...sample(0, 1, false), rows: [{ v: 'pass row' }] }
    const violationSample = { ...sample(0, 1, false), rows: [{ v: 'violation row' }] }
    apiMock.preview.mockResolvedValueOnce(passSample)
    apiMock.runOutputSample.mockResolvedValueOnce(passSample)
    useStore.setState({
      doc, canvasRole: 'owner', profileJobs: {},
      previews: { target: boundPreview(doc, 'target', violationSample, 'out') },
      runs: { target: { phase: 'done', status: {
        runId: 'named-output-run', status: 'done', targetNodeId: 'target', rowsProcessed: 2,
        totalRows: null, ms: 10, placement: 'local', perNode: [],
        outputs: [
          committedOutput('/outputs/pass.parquet', 1, 'pass'),
          committedOutput('/outputs/violations.parquet', 1, 'out'),
        ],
      } } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    expect(screen.getByRole('button', { name: 'Violations' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('violation row')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Passing' }))
    expect(await screen.findByText('pass row')).toBeInTheDocument()
    expect(apiMock.preview).toHaveBeenLastCalledWith(doc, 'target', 50, 0, 'pass')

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await waitFor(() => expect(apiMock.profile).toHaveBeenLastCalledWith(doc, 'target', 'pass'))
    expect(screen.getByRole('button', { name: 'full dataset' })).toBeEnabled()
    apiMock.profileEstimate.mockResolvedValueOnce({
      rows: null, bytes: null, placement: 'local', needsConfirm: true,
      targetPortId: 'pass', planDigest: 'b'.repeat(64),
    })
    await user.click(screen.getByRole('button', { name: 'full dataset' }))
    await user.click(screen.getByRole('button', { name: 'Estimate full profile' }))
    await waitFor(() => expect(apiMock.profileEstimate).toHaveBeenCalledWith(doc, 'target', 'pass'))
    await user.click(screen.getByRole('button', { name: 'Rows' }))
    await user.click(screen.getByRole('button', { name: 'Full result' }))
    await waitFor(() => expect(apiMock.runOutputSample).toHaveBeenLastCalledWith(
      'named-output-run', 'target', 'pass', 50, 0,
    ))
  })

  it('keeps a committed named output readable after an overall failed run', async () => {
    registerAssertUiTestNode()
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'assert-ui-test', position: { x: 0, y: 0 },
      data: { title: 'Quality gate', status: 'failed', config: {}, history: [] },
    }] }
    const passSample = { ...sample(0, 1, false), rows: [{ v: 'failed artifact survived' }] }
    apiMock.preview.mockResolvedValueOnce(passSample)
    apiMock.runOutputSample.mockResolvedValueOnce(passSample)
    useStore.setState({
      doc,
      previews: { target: boundPreview(doc, 'target', { ...sample(0, 1, false), rows: [{ v: 'violation preview' }] }, 'out') },
      runs: { target: { phase: 'failed', status: {
        runId: 'failed-named-output-run', status: 'failed', targetNodeId: 'target',
        rowsProcessed: 999, totalRows: null, ms: 10, placement: 'local', perNode: [],
        error: 'one named output failed',
        outputs: [
          committedOutput('/outputs/failed-pass.parquet', 1, 'pass'),
          {
            nodeId: 'target', portId: 'out', portLabel: 'Violations', wire: 'dataset',
            publicationKind: 'result', outcome: 'failed', error: 'writer failed',
          },
        ],
      } } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    expect(within(screen.getByRole('button', { name: 'Passing' })).getByText('committed')).toBeInTheDocument()
    expect(within(screen.getByRole('button', { name: 'Violations' })).getByText('failed')).toBeInTheDocument()
    expect(screen.getByLabelText('Selected output status')).toHaveTextContent(/Latest run\s*failed/)
    expect(screen.getByText('writer failed')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Passing' }))
    expect(await screen.findByText('failed artifact survived')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Full result' }))
    await waitFor(() => expect(apiMock.runOutputSample).toHaveBeenLastCalledWith(
      'failed-named-output-run', 'target', 'pass', 50, 0,
    ))
  })

  it('does not carry a same-named port selection across a nodeId change', () => {
    registerAssertUiTestNode()
    const first = {
      id: 'first', type: 'assert-ui-test', position: { x: 0, y: 0 },
      data: { title: 'First gate', status: 'latest', config: {}, history: [] },
    }
    const second = {
      id: 'second', type: 'assert-ui-test', position: { x: 0, y: 0 },
      data: { title: 'Second gate', status: 'latest', config: {}, history: [] },
    }
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [first, second] }
    useStore.setState({
      doc, runs: {},
      previews: {
        first: boundPreview(doc, 'first', {
          columns: [{ name: 'v', type: 'BIGINT', capabilities: [] }], rows: [{ v: 'first pass' }], truncated: false,
        }, 'pass'),
        second: boundPreview(doc, 'second', {
          columns: [{ name: 'v', type: 'BIGINT', capabilities: [] }], rows: [{ v: 'second violations' }], truncated: false,
        }, 'out'),
      },
    } as any)

    const { rerender } = render(<DataPanel nodeId="first" />)
    expect(screen.getByRole('button', { name: 'Passing' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('first pass')).toBeInTheDocument()

    rerender(<DataPanel nodeId="second" />)
    expect(screen.getByRole('button', { name: 'Violations' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('second violations')).toBeInTheDocument()
    expect(apiMock.preview).not.toHaveBeenCalled()
  })

  it('keeps each Section port explicitly not-previewable', async () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'section', position: { x: 0, y: 0 },
      data: { title: 'Branches', status: 'stale', config: { outputs: ['left', 'right'] }, history: [] },
    }] }
    apiMock.preview.mockResolvedValueOnce({
      columns: [], rows: [], truncated: false, notPreviewable: true, reason: 'right requires a full pass',
    })
    useStore.setState({
      doc, runs: {},
      previews: { target: boundPreview(doc, 'target', {
        columns: [], rows: [], truncated: false, notPreviewable: true, reason: 'left requires a full pass',
      }, 'left') },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    expect(screen.getByText(/left requires a full pass/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Run a full pass/i })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'right' }))
    expect(await screen.findByText(/right requires a full pass/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Run a full pass/i })).toBeInTheDocument()
    expect(apiMock.preview).toHaveBeenLastCalledWith(doc, 'target', 50, 0, 'right')
  })

  it('renders an exact grouped-chart artifact as a chart without starting another scan', async () => {
    apiMock.runOutputSample.mockResolvedValue({
      columns: [
        { name: 'x', type: 'VARCHAR', capabilities: [] },
        { name: 'y', type: 'BIGINT', capabilities: [] },
      ],
      rows: [{ x: 'walk', y: 4 }, { x: 'pick', y: 7 }],
      rowCount: 2, hasMore: false, truncated: false, completeness: 'complete',
    })
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'chart', position: { x: 0, y: 0 },
      data: { title: 'Tasks', status: 'latest', config: { chartType: 'bar', x: 'task', y: 'count', agg: 'sum' }, history: [] },
    }] }
    useStore.setState({
      doc, canvasRole: 'owner', profileJobs: {},
      previews: { target: boundPreview(doc, 'target', {
        columns: [], rows: [], truncated: false, notPreviewable: true,
        reason: 'grouped charts require a full pass',
      }) },
      runs: { target: { phase: 'done', status: {
        runId: 'chart-exact-run', status: 'done', targetNodeId: 'target',
        rowsProcessed: 2, totalRows: 2, ms: 10, placement: 'local', perNode: [],
        outputs: [committedOutput('/outputs/grouped-chart.parquet', 2)],
      } } },
    } as any)

    render(<DataPanel nodeId="target" />)

    expect(await screen.findByRole('img', { name: 'bar chart, complete groups' })).toBeInTheDocument()
    expect(screen.getByText('sum(count) vs task · 2 groups · Complete full result')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Preview sample' })).toBeInTheDocument()
    expect(apiMock.runOutputSample).toHaveBeenCalledWith('chart-exact-run', 'target', 'out', 2_000, 0)
    expect(apiMock.fullProfile).not.toHaveBeenCalled()
  })

  it('omits null, blank, and non-numeric chart values instead of fabricating zeroes', async () => {
    apiMock.runOutputSample.mockResolvedValue({
      columns: [
        { name: 'x', type: 'VARCHAR', capabilities: [] },
        { name: 'y', type: 'DOUBLE', capabilities: [] },
      ],
      rows: [
        { x: 'null-value', y: null },
        { x: 'blank-value', y: '   ' },
        { x: 'bad-value', y: 'not-a-number' },
        { x: 'valid-value', y: 7 },
      ],
      rowCount: 4, hasMore: false, truncated: false, completeness: 'complete',
    })
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'chart', position: { x: 0, y: 0 },
      data: { title: 'Truthful chart', status: 'latest', config: { chartType: 'bar', x: 'kind', y: 'value', agg: 'sum' }, history: [] },
    }] }
    useStore.setState({
      doc, canvasRole: 'owner', profileJobs: {},
      previews: { target: boundPreview(doc, 'target', {
        columns: [], rows: [], truncated: false, completeness: 'unknown',
        notPreviewable: true, reason: 'grouped charts require a full pass',
      }) },
      runs: { target: { phase: 'done', status: {
        runId: 'chart-invalid-y-run', status: 'done', targetNodeId: 'target',
        rowsProcessed: 4, totalRows: 4, ms: 10, placement: 'local', perNode: [],
        outputs: [committedOutput('/outputs/chart-invalid-y.parquet', 4)],
      } } },
    } as any)

    const { container } = render(<DataPanel nodeId="target" />)

    expect(await screen.findByRole('img', { name: 'bar chart, complete group' })).toBeInTheDocument()
    expect(container.querySelectorAll('svg rect')).toHaveLength(1)
    expect(screen.getByText('3 Y values omitted because they were null, blank, or non-numeric.')).toBeInTheDocument()
    expect(screen.getByText('valid-valu')).toBeInTheDocument()
    expect(screen.queryByText('null-value')).not.toBeInTheDocument()
  })

  it('states when a chart artifact has no numeric Y values', async () => {
    apiMock.runOutputSample.mockResolvedValue({
      columns: [
        { name: 'x', type: 'VARCHAR', capabilities: [] },
        { name: 'y', type: 'DOUBLE', capabilities: [] },
      ],
      rows: [{ x: 'null-value', y: null }, { x: 'bad-value', y: 'nope' }],
      rowCount: 2, hasMore: false, truncated: false, completeness: 'complete',
    })

    render(<FullResult uri="/outputs/no-numeric-y.parquet" total={2} {...fullIdentity}
      presentation={{ kind: 'chart', type: 'bar', xLabel: 'kind', yLabel: 'value', grouped: true }} />)

    expect(await screen.findByText('No numeric Y values to chart.')).toBeInTheDocument()
    expect(screen.getByText('2 Y values omitted because they were null, blank, or non-numeric.')).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('caps only the grouped-chart display while identifying the complete artifact size', async () => {
    apiMock.runOutputSample.mockResolvedValue({
      columns: [
        { name: 'x', type: 'VARCHAR', capabilities: [] },
        { name: 'y', type: 'BIGINT', capabilities: [] },
      ],
      rows: Array.from({ length: 2_000 }, (_, index) => ({ x: `group-${index}`, y: index })),
      rowCount: 2_001, hasMore: false, truncated: true,
      completeness: 'capped', rowLimit: 2_000, limitReason: 'interactive-row-budget',
    })
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'chart', position: { x: 0, y: 0 },
      data: { title: 'Tasks', status: 'latest', config: { chartType: 'bar', x: 'task', y: 'count', agg: 'sum' }, history: [] },
    }] }
    useStore.setState({
      doc, canvasRole: 'owner', profileJobs: {},
      previews: { target: boundPreview(doc, 'target', {
        columns: [], rows: [], truncated: false, completeness: 'unknown',
        notPreviewable: true, reason: 'grouped charts require a full pass',
      }) },
      runs: { target: { phase: 'done', status: {
        runId: 'chart-capped-run', status: 'done', targetNodeId: 'target',
        rowsProcessed: 2_001, totalRows: 2_001, ms: 10, placement: 'local', perNode: [],
        outputs: [committedOutput('/outputs/grouped-chart-many.parquet', 2_001)],
      } } },
    } as any)

    render(<DataPanel nodeId="target" />)

    expect(await screen.findByRole('img', {
      name: 'bar chart, showing 2000 capped groups',
    })).toBeInTheDocument()
    expect(screen.getByText(
      'sum(count) vs task · Showing 2,000 of 2,001 groups · display capped',
    )).toBeInTheDocument()
    expect(screen.getByText(/Interactive view stopped at 2,000 groups of 2,001/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export full result' })).toBeInTheDocument()
    expect(apiMock.runOutputSample).toHaveBeenCalledWith(
      'chart-capped-run', 'target', 'out', 2_000, 0,
    )
  })

  it('renders an exact metric artifact as a scalar without starting another scan', async () => {
    apiMock.runOutputSample.mockResolvedValue({
      columns: [
        { name: 'metric', type: 'VARCHAR', capabilities: [] },
        { name: 'value', type: 'BIGINT', capabilities: [] },
      ],
      rows: [{ metric: 'successful grasps', value: 1234 }],
      rowCount: 1, hasMore: false, truncated: false, completeness: 'complete',
    })
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'metric', position: { x: 0, y: 0 },
      data: { title: 'Successes', status: 'latest', config: {}, history: [] },
    }] }
    useStore.setState({
      doc, canvasRole: 'owner', profileJobs: {},
      previews: { target: boundPreview(doc, 'target', {
        columns: [], rows: [], truncated: false, notPreviewable: true,
        reason: 'this metric requires a full pass',
      }) },
      runs: { target: { phase: 'done', status: {
        runId: 'metric-exact-run', status: 'done', targetNodeId: 'target',
        rowsProcessed: 1, totalRows: 1, ms: 10, placement: 'local', perNode: [],
        outputs: [committedOutput('/outputs/metric.parquet', 1)],
      } } },
    } as any)

    render(<DataPanel nodeId="target" />)

    expect(await screen.findByText('1,234')).toBeInTheDocument()
    expect(screen.getByText('successful grasps · over the full dataset')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Preview sample' })).toBeInTheDocument()
    expect(apiMock.runOutputSample).toHaveBeenCalledWith('metric-exact-run', 'target', 'out', 50, 0)
    expect(apiMock.fullProfile).not.toHaveBeenCalled()
  })

  it('blocks stale rows and offers a refresh for the current graph', () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'filter', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'stale', config: { predicate: 'event = view' }, history: [] },
    }] }
    useStore.setState({
      doc,
      previews: {
        target: {
          canvasId: doc.id, nodeId: 'target', planIdentity: 'a-previous-plan', requestGeneration: 1,
          offset: 0,
          result: { columns: [{ name: 'event', type: 'VARCHAR', capabilities: [] }], rows: [{ event: 'purchase' }], rowCount: 1, hasMore: false, truncated: false },
        },
      },
    } as any)

    render(<DataPanel nodeId="target" />)
    expect(screen.getByRole('status')).toHaveTextContent('Preview out of date')
    expect(screen.getByRole('button', { name: 'Refresh preview' })).toBeInTheDocument()
    expect(screen.queryByText('purchase')).not.toBeInTheDocument()
  })

  it('describes the graph preview limit as a per-source scan cap, not an output prefix', () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'filter', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: { predicate: 'score > 0' }, history: [] },
    }] }
    useStore.setState({
      doc,
      previews: { target: boundPreview(doc, 'target', {
        ...sample(0, 1, false), completeness: 'sample', rowLimit: 2_000,
        limitReason: 'preview-scan', limitScope: 'each-source',
      }) },
    } as any)

    render(<DataPanel nodeId="target" />)

    expect(screen.getByText(/Each source read was limited to at most 2,000 rows before this preview was computed/)).toBeInTheDocument()
    expect(screen.getByText(/Output rows are not necessarily the first 2,000 rows of the final result/)).toBeInTheDocument()
    expect(screen.queryByText(/preview did not inspect beyond the first/i)).not.toBeInTheDocument()
  })

  it('keeps sample-profile responses bound to the latest execution plan', async () => {
    let finishOld!: (value: any) => void
    let finishCurrent!: (value: any) => void
    apiMock.profile
      .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishCurrent = resolve }))
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'filter', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: { predicate: 'event = purchase' }, history: [] },
    }] }
    useStore.setState({
      doc, canvasRole: 'owner', profileJobs: {},
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await waitFor(() => expect(apiMock.profile).toHaveBeenCalledTimes(1))

    const edited = structuredClone(doc)
    edited.nodes[0].data.config.predicate = 'event = view'
    act(() => useStore.setState({
      doc: edited,
      previews: { target: boundPreview(edited, 'target', sample(0, 10, false)) },
    } as any))
    await waitFor(() => expect(apiMock.profile).toHaveBeenCalledTimes(2))

    await act(async () => finishCurrent({
      columns: [], rowCount: 20, sampled: true, completeness: 'sample',
    }))
    expect(await screen.findByText('Preview sample · 20 rows inspected')).toBeInTheDocument()

    await act(async () => finishOld({
      columns: [], rowCount: 10, sampled: true, completeness: 'sample',
    }))
    expect(screen.getByText('Preview sample · 20 rows inspected')).toBeInTheDocument()
    expect(screen.queryByText('Preview sample · 10 rows inspected')).not.toBeInTheDocument()

    const moved = structuredClone(edited)
    moved.nodes[0].position = { x: 500, y: 300 }
    moved.nodes[0].data.status = 'running'
    act(() => useStore.setState({ doc: moved }))
    await Promise.resolve()
    expect(apiMock.profile).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Preview sample · 20 rows inspected')).toBeInTheDocument()
  })

  it('keeps sample-profile responses bound to the latest parameter binding', async () => {
    let finishOld!: (value: any) => void
    let finishCurrent!: (value: any) => void
    apiMock.profile
      .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishCurrent = resolve }))
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [],
      parameters: [{ name: 'threshold', type: 'integer' as const, required: true }], edges: [], nodes: [{
        id: 'target', type: 'filter', position: { x: 0, y: 0 },
        data: { title: 'target', status: 'latest', config: { threshold: { parameterRef: 'threshold' } }, history: [] },
      }] }
    const first = [{ name: 'threshold', value: 1 }]
    const second = [{ name: 'threshold', value: 2 }]
    useStore.setState({
      doc, canvasRole: 'owner', profileJobs: {},
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
      runs: { target: { phase: 'idle', parameterBindings: first } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await waitFor(() => expect(apiMock.profile).toHaveBeenCalledTimes(1))
    act(() => useStore.setState((state) => ({ runs: { ...state.runs, target: {
      ...state.runs.target!, parameterBindings: second,
    } } })))
    await waitFor(() => expect(apiMock.profile).toHaveBeenCalledTimes(2))

    await act(async () => finishCurrent({
      columns: [], rowCount: 20, sampled: true, completeness: 'sample',
    }))
    expect(await screen.findByText('Preview sample · 20 rows inspected')).toBeInTheDocument()
    await act(async () => finishOld({
      columns: [], rowCount: 10, sampled: true, completeness: 'sample',
    }))
    expect(screen.queryByText('Preview sample · 10 rows inspected')).not.toBeInTheDocument()
    expect(apiMock.profile).toHaveBeenNthCalledWith(1, doc, 'target', undefined, undefined, first)
    expect(apiMock.profile).toHaveBeenNthCalledWith(2, doc, 'target', undefined, undefined, second)
  })

  it('hides a completed full profile produced with an older parameter binding', async () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [],
      parameters: [{ name: 'threshold', type: 'integer' as const, required: true }], edges: [], nodes: [{
        id: 'target', type: 'filter', position: { x: 0, y: 0 },
        data: { title: 'target', status: 'latest', config: { threshold: { parameterRef: 'threshold' } }, history: [] },
      }] }
    const first = [{ name: 'threshold', value: 1 }]
    const second = [{ name: 'threshold', value: 2 }]
    useStore.setState({
      doc, canvasRole: 'owner',
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
      runs: { target: { phase: 'idle', parameterBindings: second } },
      profileJobs: { target: {
        canvasId: doc.id, nodeId: 'target', portId: 'out', principalId: 'alice',
        parameterBindings: first, planIdentity: profilePlanIdentity(doc, 'target', 'out'),
        requestGeneration: 1, phase: 'done', identityVerified: true,
        status: {
          runId: 'old-binding-profile', status: 'done', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
          planDigest: 'a'.repeat(64), profileAttemptOrder: 1, rowsProcessed: 999, totalRows: 999,
          ms: 10, placement: 'local', perNode: [], outputs: [],
          profile: { columns: [], rowCount: 999, sampled: false, completeness: 'complete' },
        },
      } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await user.click(screen.getByRole('button', { name: 'full dataset' }))
    expect(screen.getByRole('button', { name: 'Estimate full profile' })).toBeInTheDocument()
    expect(screen.queryByText(/999 rows scanned/)).not.toBeInTheDocument()
  })

  it('does not guess profile scope when the kernel reports unknown completeness', async () => {
    apiMock.profile.mockResolvedValueOnce({
      columns: [{
        name: 'score', type: 'DOUBLE', nonNull: 9, nulls: 1, distinct: 4,
        distinctIsApproximate: false, min: '1', max: '9', mean: 5,
      }],
      rowCount: 10, sampled: true, completeness: 'unknown', notPreviewable: false,
    })
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    useStore.setState({
      doc, canvasRole: 'owner', profileJobs: {},
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))

    expect(await screen.findByText('Profile scope unknown · 10 rows reported')).toBeInTheDocument()
    expect(screen.getByText(/did not report whether these statistics cover a sample or the whole dataset/i)).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Distinct' })).toBeInTheDocument()
    expect(screen.queryByText(/Whole dataset · 10 rows/)).not.toBeInTheDocument()
  })

  it('marks only explicitly approximate distinct counts in a complete profile', async () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    const planDigest = 'a'.repeat(64)
    useStore.setState({
      doc, canvasRole: 'owner',
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
      profileJobs: { target: {
        canvasId: doc.id, nodeId: 'target', principalId: 'alice', canCancel: false,
        planIdentity: profilePlanIdentity(doc, 'target'), planDigest,
        requestGeneration: 1, phase: 'done', identityVerified: true,
        status: {
          runId: 'profile-distinct-compat', status: 'done', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
          planDigest, profileAttemptOrder: 1, rowsProcessed: 10, totalRows: 10,
          ms: 10, placement: 'local', perNode: [],
          profile: {
            columns: [
              { name: 'exact_task', type: 'VARCHAR', nonNull: 10, nulls: 0, distinct: 7, distinctIsApproximate: false },
              { name: 'approx_task', type: 'VARCHAR', nonNull: 10, nulls: 0, distinct: 8, distinctIsApproximate: true },
            ],
            rowCount: 10, sampled: false, completeness: 'complete', notPreviewable: false,
          },
        },
      } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await user.click(screen.getByRole('button', { name: 'full dataset' }))

    expect(screen.getByRole('columnheader', { name: 'Distinct' })).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
    expect(screen.queryByLabelText('Estimated distinct count: 7')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Estimated distinct count: 8')).toHaveTextContent('≈ 8')
  })

  it('shows profile preflight before an explicit confirmed start', async () => {
    apiMock.profileEstimate.mockResolvedValueOnce({
      rows: 10, bytes: 5 * 1024 ** 3, placement: 'local', needsConfirm: true,
      targetPortId: 'out', planDigest: 'a'.repeat(64),
    })
    let finishProfile!: (status: any) => void
    apiMock.fullProfile.mockImplementationOnce(() => new Promise((resolve) => { finishProfile = resolve }))
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    useStore.setState({
      doc,
      canvasRole: 'owner',
      profileJobs: {},
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await user.click(screen.getByRole('button', { name: 'full dataset' }))
    expect(screen.getByText('Whole-dataset profile')).toBeInTheDocument()
    expect(apiMock.profileEstimate).not.toHaveBeenCalled()
    expect(apiMock.fullProfile).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Estimate full profile' }))
    expect(await screen.findByText('Profile preflight')).toBeInTheDocument()
    expect(screen.getByText(/Estimated 10 rows · 5 GiB · Large or unknown scan/i)).toBeInTheDocument()
    expect(screen.getByText(/distinct counts are approximate/i)).toBeInTheDocument()
    expect(apiMock.fullProfile).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Start whole-dataset profile' }))
    await waitFor(() => expect(apiMock.fullProfile).toHaveBeenCalledWith(
      doc, 'target', 'out', expect.any(String), expect.any(String), true,
    ))
    expect(screen.getByText('Full profile queued…')).toBeInTheDocument()
    expect(screen.getByText(/Estimated 10 rows · 5 GiB · whole-dataset scan/i)).toBeInTheDocument()

    finishProfile({
      runId: 'profile-ui', status: 'done', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false, completeness: 'complete' },
    })
    expect(await screen.findByText('Whole dataset · 10 rows scanned')).toBeInTheDocument()
  })

  it('lets viewers read recovered full profiles without mutation controls', async () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    const planIdentity = profilePlanIdentity(doc, 'target')
    const planDigest = 'a'.repeat(64)
    const done = {
      runId: 'profile-viewer', status: 'done', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false, completeness: 'complete' },
    }
    useStore.setState({
      doc,
      canvasRole: 'viewer',
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
      profileJobs: { target: {
        canvasId: doc.id, nodeId: 'target', principalId: 'alice', canCancel: false,
        planIdentity, planDigest,
        requestGeneration: 1, phase: 'done', status: done,
      } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await user.click(screen.getByRole('button', { name: 'full dataset' }))
    expect(screen.getByText('Whole dataset · 10 rows scanned')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Estimate full profile' })).not.toBeInTheDocument()
    expect(apiMock.profileEstimate).not.toHaveBeenCalled()
    expect(apiMock.fullProfile).not.toHaveBeenCalled()

    act(() => useStore.setState((state) => ({ profileJobs: { ...state.profileJobs, target: {
      ...state.profileJobs.target!, phase: 'verifying', identityVerified: false,
      status: { ...done, status: 'running', profile: undefined },
    } } })))
    expect(screen.getByText('Verifying recovered full profile…')).toBeInTheDocument()
    expect(screen.getByText('Statistics remain hidden until verification completes.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(apiMock.cancelRun).not.toHaveBeenCalled()
  })

  it('hides unverified recovered statistics but keeps the exact active run cancellable for editors', async () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    const planDigest = 'a'.repeat(64)
    useStore.setState({
      doc,
      canvasRole: 'owner',
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
      profileJobs: { target: {
        canvasId: doc.id, nodeId: 'target', principalId: 'alice', canCancel: true,
        planIdentity: profilePlanIdentity(doc, 'target'), planDigest,
        requestGeneration: 1, phase: 'verifying', identityVerified: false,
        status: {
          runId: 'profile-unverified-active', status: 'running', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
          planDigest, profileAttemptOrder: 2, rowsProcessed: 5, ms: 10,
          placement: 'local', perNode: [],
          profile: { columns: [], rowCount: 999, sampled: false, completeness: 'complete' },
          outputs: [committedOutput('/unverified/result.parquet', 999)],
        },
      } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await user.click(screen.getByRole('button', { name: 'full dataset' }))

    expect(screen.getByText('Verifying recovered full profile…')).toBeInTheDocument()
    expect(screen.getByText('Statistics remain hidden until verification completes.')).toBeInTheDocument()
    expect(screen.queryByText('Full profile not verified')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.queryByText(/whole dataset · 999 rows/i)).not.toBeInTheDocument()
    apiMock.cancelRun.mockResolvedValueOnce({
      runId: 'profile-unverified-active', status: 'cancelled', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
      planDigest, profileAttemptOrder: 2, rowsProcessed: 5, ms: 10,
      placement: 'local', perNode: [],
    })
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(apiMock.cancelRun).toHaveBeenCalledWith('profile-unverified-active')
  })

  it.each([
    ['owner', true],
    ['viewer', false],
  ] as const)('shows terminal recovery verification as progress without mutations for %s', async (role, canCancel) => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    const planDigest = 'a'.repeat(64)
    useStore.setState({
      doc,
      canvasRole: role,
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
      profileJobs: { target: {
        canvasId: doc.id, nodeId: 'target', principalId: 'alice', canCancel,
        planIdentity: profilePlanIdentity(doc, 'target'), planDigest,
        requestGeneration: 1, phase: 'verifying', identityVerified: false,
        status: {
          runId: `profile-terminal-verifying-${role}`, status: 'done', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
          planDigest, profileAttemptOrder: 2, rowsProcessed: 10, ms: 10,
          placement: 'local', perNode: [],
        },
      } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await user.click(screen.getByRole('button', { name: 'full dataset' }))

    expect(screen.getByText('Verifying recovered full profile…')).toBeInTheDocument()
    expect(screen.getByText('Statistics remain hidden until verification completes.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.queryByText('Full profile not verified')).not.toBeInTheDocument()
    expect(apiMock.cancelRun).not.toHaveBeenCalled()
  })

  it('keeps whole-dataset mode reachable when sample statistics are not previewable', async () => {
    apiMock.profile.mockResolvedValueOnce({
      columns: [], rowCount: 0, sampled: true, notPreviewable: true,
      reason: 'sort statistics require a full pass',
    })
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'sort', position: { x: 0, y: 0 },
      data: { title: 'Sorted rows', status: 'latest', config: { by: 'score' }, history: [] },
    }] }
    useStore.setState({
      doc, canvasRole: 'owner', profileJobs: {},
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    expect(await screen.findByText('Not sample-previewable')).toBeInTheDocument()
    expect(screen.getByText(/switch to full dataset to estimate/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /run a full pass/i })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'full dataset' }))
    await user.click(screen.getByRole('button', { name: 'Estimate full profile' }))
    await waitFor(() => expect(apiMock.profileEstimate).toHaveBeenCalledWith(doc, 'target', 'out'))
  })

  it('hides Alice full-profile statistics synchronously when identity changes to Bob', async () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    const planDigest = 'a'.repeat(64)
    useStore.setState({
      doc, canvasRole: 'owner',
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
      profileJobs: { target: {
        canvasId: doc.id, nodeId: 'target', principalId: 'alice', canCancel: true,
        planIdentity: profilePlanIdentity(doc, 'target'), planDigest,
        requestGeneration: 1, phase: 'done', identityVerified: true,
        status: {
          runId: 'alice-visible-profile', status: 'done', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
          planDigest, profileAttemptOrder: 1, rowsProcessed: 10, totalRows: 10,
          ms: 10, placement: 'local', perNode: [],
          profile: { columns: [], rowCount: 10, sampled: false, completeness: 'complete' },
        },
      } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)
    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await user.click(screen.getByRole('button', { name: 'full dataset' }))
    expect(screen.getByText('Whole dataset · 10 rows scanned')).toBeInTheDocument()

    act(() => useStore.setState({ currentUser: { id: 'bob', name: 'Bob' } }))

    expect(screen.queryByText('Whole dataset · 10 rows scanned')).not.toBeInTheDocument()
    expect(screen.getByText('Whole-dataset profile')).toBeInTheDocument()
  })

  it('labels a failed whole-dataset job separately from sample preview failures', async () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    const planIdentity = profilePlanIdentity(doc, 'target')
    const planDigest = 'a'.repeat(64)
    useStore.setState({
      doc,
      canvasRole: 'owner',
      previews: { target: boundPreview(doc, 'target', sample(0, 10, false)) },
      profileJobs: { target: {
        canvasId: doc.id, nodeId: 'target', principalId: 'alice', canCancel: true,
        planIdentity, planDigest,
        requestGeneration: 1, phase: 'failed', error: 'adapter timed out',
        status: {
          runId: 'profile-failed', status: 'failed', jobType: 'profile', targetNodeId: 'target', targetPortId: 'out',
          planDigest, profileAttemptOrder: 1, rowsProcessed: 0, ms: 10,
          placement: 'local', perNode: [], error: 'adapter timed out',
        },
      } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)

    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await user.click(screen.getByRole('button', { name: 'full dataset' }))

    expect(screen.getByText('Full profile failed')).toBeInTheDocument()
    expect(screen.queryByText('Preview failed')).not.toBeInTheDocument()
    expect(screen.getByText('adapter timed out')).toBeInTheDocument()
  })
})

function boundPreview(doc: any, nodeId: string, result: any, portId?: string) {
  return { canvasId: doc.id, nodeId, portId, planIdentity: previewPlanIdentity(doc, nodeId, portId), requestGeneration: 1, offset: 0, result }
}
