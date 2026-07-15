import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { fmtMs, DurationTrend, PerNodeBreakdown } from './RunHistoryModal'
import type { PerNodeStat, RunRecordDto } from '../api/client'
import { RunHistoryModal } from './RunHistoryModal'
import { DataPanel, FullResult } from './DataPanel'
import { previewPlanIdentity, useStore } from '../store/graph'

const apiMock = vi.hoisted(() => ({
  listRuns: vi.fn(),
  sample: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: apiMock }))

beforeEach(() => {
  apiMock.listRuns.mockReset()
  apiMock.sample.mockReset()
  useStore.setState({
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
    { id: 'r2', status: 'failed', ms: 200 },
    { id: 'r1', status: 'done', ms: 100 },
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
    { node_id: 'src', label: 'source', status: 'done', ms: 10, rows: 5 },
    { node_id: 'wr', label: 'write', status: 'done', ms: 90, rows: 5 },
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
  const sample = (offset: number, count: number, hasMore: boolean) => ({
    columns: [{ name: 'v', type: 'BIGINT', capabilities: [] }],
    rows: Array.from({ length: count }, (_, i) => ({ v: offset + i })),
    rowCount: 105,
    hasMore,
    truncated: hasMore,
  })

  it('reopens a completed result from persisted run history', async () => {
    apiMock.listRuns.mockResolvedValue([{ id: 'history-row', runId: 'run-real', status: 'done',
      targetNodeId: 'target', rows: 105, outputUri: '/outputs/result.parquet' }])
    apiMock.sample.mockResolvedValue(sample(0, 50, true))
    const user = userEvent.setup()
    render(<RunHistoryModal onClose={() => {}} />)

    await user.click(await screen.findByRole('button', { name: 'Open full result' }))
    expect(await screen.findByText('rows 1–50 of 105')).toBeInTheDocument()
    expect(apiMock.sample).toHaveBeenCalledWith('/outputs/result.parquet', 50, undefined, 0)
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
        perNode: [], outputUri: '/outputs/result.parquet' } } },
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
        perNode: [], outputUri: '/outputs/result.parquet' } } },
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
})

function boundPreview(doc: any, nodeId: string, result: any) {
  return { canvasId: doc.id, nodeId, planIdentity: previewPlanIdentity(doc, nodeId), requestGeneration: 1, offset: 0, result }
}
