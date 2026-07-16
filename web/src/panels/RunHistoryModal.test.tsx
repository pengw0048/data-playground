import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { fmtMs, DurationTrend, PerNodeBreakdown } from './RunHistoryModal'
import type { PerNodeStat, RunRecordDto } from '../api/client'
import { RunHistoryModal } from './RunHistoryModal'
import { DataPanel, FullResult } from './DataPanel'
import { previewPlanIdentity, profilePlanIdentity, useStore } from '../store/graph'
import { register } from '../nodes/registry'

const apiMock = vi.hoisted(() => ({
  listRuns: vi.fn(),
  sample: vi.fn(),
  preview: vi.fn(),
  profile: vi.fn(),
  profileEstimate: vi.fn(),
  fullProfile: vi.fn(),
  cancelRun: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: apiMock }))

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
  apiMock.sample.mockReset()
  apiMock.preview.mockReset()
  apiMock.profile.mockReset().mockResolvedValue({ columns: [], rowCount: 10, sampled: true })
  apiMock.profileEstimate.mockReset().mockResolvedValue({
    rows: null, bytes: null, placement: 'local', needsConfirm: true, planDigest: 'a'.repeat(64),
  })
  apiMock.fullProfile.mockReset().mockResolvedValue({
    runId: 'profile-ui', status: 'done', jobType: 'profile', targetNodeId: 'target',
    planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
    rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
    profile: { columns: [], rowCount: 10, sampled: false },
  })
  apiMock.cancelRun.mockReset().mockImplementation(async (runId: string) => ({
    runId, status: 'cancelled', jobType: 'run',
    rowsProcessed: 0, ms: 0, placement: 'local', perNode: [],
  }))
  useStore.setState({
    currentUser: { id: 'alice', name: 'Alice' },
    doc: { id: 'history-canvas', name: 'History', version: 1, nodes: [], edges: [], requirements: [] },
    previews: {}, runs: {},
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
  })

  it('reopens a completed result from persisted run history', async () => {
    apiMock.listRuns.mockResolvedValue([{ id: 'history-row', runId: 'run-real', status: 'done',
      targetNodeId: 'target', rows: 105, outputs: [committedOutput('/outputs/result.parquet', 105)] }])
    apiMock.sample.mockResolvedValue(sample(0, 50, true))
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    await user.click(await screen.findByRole('button', { name: 'Open full result' }))
    expect(await screen.findByText('rows 1–50 of 105')).toBeInTheDocument()
    expect(apiMock.sample).toHaveBeenCalledWith('/outputs/result.parquet', 50, undefined, 0)
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
    apiMock.sample.mockResolvedValue({ ...sample(0, 7, false), rows: [{ v: 'survived' }] })
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    expect(await screen.findByLabelText('Outputs for run partial-history')).toBeInTheDocument()
    expect(screen.getByText('pass')).toBeInTheDocument()
    expect(screen.getByText('Violations')).toBeInTheDocument()
    expect(screen.getByText('writer failed')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open pass' }))
    expect(await screen.findByText('survived')).toBeInTheDocument()
  })

  it('pages beyond the first 50 materialized rows', async () => {
    apiMock.sample.mockImplementation(async (_uri: string, _k: number, _columns: unknown, offset: number) =>
      offset === 0 ? sample(0, 50, true) : sample(50, 50, true))
    const user = userEvent.setup()
    // History can hold rows written by one append attempt (50), while the reopened artifact is
    // cumulative (105). The artifact's measured rowCount is authoritative for display/paging.
    render(<FullResult uri="/outputs/result.parquet" total={50} />)
    await screen.findByText('rows 1–50 of 105')

    await user.click(screen.getByRole('button', { name: 'Next page' }))
    expect(await screen.findByText('rows 51–100 of 105')).toBeInTheDocument()
    expect(apiMock.sample).toHaveBeenLastCalledWith('/outputs/result.parquet', 50, undefined, 50)
  })

  it('stops at the read budget while preserving truncated export labeling', async () => {
    apiMock.sample.mockImplementation(async (
      _uri: string, _k: number, _columns: unknown, offset: number,
    ) => ({
      ...sample(offset, 50, offset < 1950),
      rowCount: 100_000,
      truncated: true,
    }))
    const user = userEvent.setup()

    render(<FullResult uri="/outputs/budget-terminal.parquet" total={100_000} />)

    await screen.findByText('rows 1–50 of 100,000')
    expect(screen.queryByText('Interactive view limit reached')).not.toBeInTheDocument()
    for (let offset = 50; offset <= 1950; offset += 50) {
      await user.click(screen.getByRole('button', { name: 'Next page' }))
      await screen.findByText(`rows ${offset + 1}–${offset + 50} of 100,000`)
    }
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled()
    expect(screen.getByText('Interactive view limit reached')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'CSV' })).toHaveAttribute(
      'title',
      expect.stringContaining('previewed sample only'),
    )
  })

  it('labels a missing or expired artifact explicitly', async () => {
    apiMock.sample.mockRejectedValue(new Error('404: no such file'))
    render(<FullResult uri="/outputs/missing.parquet" total={105} />)
    expect(await screen.findByText('Full result expired or removed')).toBeInTheDocument()
    expect(screen.getByText(/stored artifact is no longer available/i)).toBeInTheDocument()
  })

  it('does not mislabel an authorization failure as expiration', async () => {
    apiMock.sample.mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }))
    render(<FullResult uri="/outputs/private.parquet" total={105} />)
    expect(await screen.findByText('Full result access denied')).toBeInTheDocument()
    expect(screen.queryByText('Full result expired or removed')).not.toBeInTheDocument()
  })

  it('offers sample/full switching for previewable nodes after a full run', async () => {
    apiMock.sample.mockResolvedValue(sample(0, 50, true))
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
    await waitFor(() => expect(apiMock.sample).toHaveBeenCalledWith('/outputs/result.parquet', 50, undefined, 0))
    expect(screen.getByRole('button', { name: 'Sample' })).toBeInTheDocument()
  })

  it('keeps the Sample escape hatch when loading Full fails temporarily', async () => {
    apiMock.sample.mockRejectedValue(Object.assign(new Error('service unavailable'), { status: 503 }))
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
    const sampleButton = screen.getByRole('button', { name: 'Sample' })
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
    apiMock.sample.mockResolvedValueOnce(passSample)
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
    expect(screen.getByRole('button', { name: 'full dataset' })).toBeDisabled()
    expect(screen.getByText(/Whole-dataset profiles are not available for multi-output nodes/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Rows' }))
    await user.click(screen.getByRole('button', { name: 'Full result' }))
    await waitFor(() => expect(apiMock.sample).toHaveBeenLastCalledWith('/outputs/pass.parquet', 50, undefined, 0))
  })

  it('keeps a committed named output readable after an overall failed run', async () => {
    registerAssertUiTestNode()
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'assert-ui-test', position: { x: 0, y: 0 },
      data: { title: 'Quality gate', status: 'failed', config: {}, history: [] },
    }] }
    const passSample = { ...sample(0, 1, false), rows: [{ v: 'failed artifact survived' }] }
    apiMock.preview.mockResolvedValueOnce(passSample)
    apiMock.sample.mockResolvedValueOnce(passSample)
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
    await waitFor(() => expect(apiMock.sample).toHaveBeenLastCalledWith(
      '/outputs/failed-pass.parquet', 50, undefined, 0,
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
    apiMock.sample.mockResolvedValue({
      columns: [
        { name: 'x', type: 'VARCHAR', capabilities: [] },
        { name: 'y', type: 'BIGINT', capabilities: [] },
      ],
      rows: [{ x: 'walk', y: 4 }, { x: 'pick', y: 7 }],
      rowCount: 2, hasMore: false, truncated: false,
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

    expect(await screen.findByRole('img', { name: 'bar chart' })).toBeInTheDocument()
    expect(screen.getByText('sum(count) vs task · 2 points')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sample' })).toBeInTheDocument()
    expect(apiMock.sample).toHaveBeenCalledWith('/outputs/grouped-chart.parquet', 50, undefined, 0)
    expect(apiMock.fullProfile).not.toHaveBeenCalled()
  })

  it('renders an exact metric artifact as a scalar without starting another scan', async () => {
    apiMock.sample.mockResolvedValue({
      columns: [
        { name: 'metric', type: 'VARCHAR', capabilities: [] },
        { name: 'value', type: 'BIGINT', capabilities: [] },
      ],
      rows: [{ metric: 'successful grasps', value: 1234 }],
      rowCount: 1, hasMore: false, truncated: false,
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
    expect(screen.getByRole('button', { name: 'Sample' })).toBeInTheDocument()
    expect(apiMock.sample).toHaveBeenCalledWith('/outputs/metric.parquet', 50, undefined, 0)
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

    await act(async () => finishCurrent({ columns: [], rowCount: 20, sampled: true }))
    expect(await screen.findByText('stats over the previewed sample · 20 rows')).toBeInTheDocument()

    await act(async () => finishOld({ columns: [], rowCount: 10, sampled: true }))
    expect(screen.getByText('stats over the previewed sample · 20 rows')).toBeInTheDocument()
    expect(screen.queryByText('stats over the previewed sample · 10 rows')).not.toBeInTheDocument()

    const moved = structuredClone(edited)
    moved.nodes[0].position = { x: 500, y: 300 }
    moved.nodes[0].data.status = 'running'
    act(() => useStore.setState({ doc: moved }))
    await Promise.resolve()
    expect(apiMock.profile).toHaveBeenCalledTimes(2)
    expect(screen.getByText('stats over the previewed sample · 20 rows')).toBeInTheDocument()
  })

  it('shows profile preflight before an explicit confirmed start', async () => {
    apiMock.profileEstimate.mockResolvedValueOnce({
      rows: 10, bytes: 5 * 1024 ** 3, placement: 'local', needsConfirm: true,
      planDigest: 'a'.repeat(64),
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
      doc, 'target', expect.any(String), expect.any(String), true,
    ))
    expect(screen.getByText('Full profile queued…')).toBeInTheDocument()
    expect(screen.getByText(/Estimated 10 rows · 5 GiB · whole-dataset scan/i)).toBeInTheDocument()

    finishProfile({
      runId: 'profile-ui', status: 'done', jobType: 'profile', targetNodeId: 'target',
      planDigest: 'a'.repeat(64), profileAttemptOrder: 1,
      rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
    })
    expect(await screen.findByText('whole dataset · 10 rows · distinct is an estimate')).toBeInTheDocument()
  })

  it('lets viewers read recovered full profiles without mutation controls', async () => {
    const doc = { id: 'history-canvas', name: 'History', version: 1, requirements: [], edges: [], nodes: [{
      id: 'target', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'target', status: 'latest', config: {}, history: [] },
    }] }
    const planIdentity = profilePlanIdentity(doc, 'target')
    const planDigest = 'a'.repeat(64)
    const done = {
      runId: 'profile-viewer', status: 'done', jobType: 'profile', targetNodeId: 'target',
      planDigest, profileAttemptOrder: 1, rowsProcessed: 10, totalRows: 10, ms: 10, placement: 'local', perNode: [],
      profile: { columns: [], rowCount: 10, sampled: false },
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
    expect(screen.getByText('whole dataset · 10 rows · distinct is an estimate')).toBeInTheDocument()
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
          runId: 'profile-unverified-active', status: 'running', jobType: 'profile', targetNodeId: 'target',
          planDigest, profileAttemptOrder: 2, rowsProcessed: 5, ms: 10,
          placement: 'local', perNode: [],
          profile: { columns: [], rowCount: 999, sampled: false },
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
      runId: 'profile-unverified-active', status: 'cancelled', jobType: 'profile', targetNodeId: 'target',
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
          runId: `profile-terminal-verifying-${role}`, status: 'done', jobType: 'profile', targetNodeId: 'target',
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
    await waitFor(() => expect(apiMock.profileEstimate).toHaveBeenCalledWith(doc, 'target'))
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
          runId: 'alice-visible-profile', status: 'done', jobType: 'profile', targetNodeId: 'target',
          planDigest, profileAttemptOrder: 1, rowsProcessed: 10, totalRows: 10,
          ms: 10, placement: 'local', perNode: [],
          profile: { columns: [], rowCount: 10, sampled: false },
        },
      } },
    } as any)
    const user = userEvent.setup()
    render(<DataPanel nodeId="target" />)
    await user.click(screen.getByRole('button', { name: 'Stats' }))
    await user.click(screen.getByRole('button', { name: 'full dataset' }))
    expect(screen.getByText('whole dataset · 10 rows · distinct is an estimate')).toBeInTheDocument()

    act(() => useStore.setState({ currentUser: { id: 'bob', name: 'Bob' } }))

    expect(screen.queryByText('whole dataset · 10 rows · distinct is an estimate')).not.toBeInTheDocument()
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
          runId: 'profile-failed', status: 'failed', jobType: 'profile', targetNodeId: 'target',
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
