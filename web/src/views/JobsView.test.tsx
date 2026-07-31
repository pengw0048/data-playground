import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  workspaceJobs: vi.fn(), executionManifest: vi.fn(), cancelRun: vi.fn(), retryRun: vi.fn(), listCanvases: vi.fn(),
  cancelMergeColumnsTask: vi.fn(), retryMergeColumnsTask: vi.fn(),
  cancelManagedSidecarMergeTask: vi.fn(), retryManagedSidecarMergeTask: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../panels/DataPanel', () => ({ FullResult: () => <div data-testid="full-result">result</div> }))

import { useStore } from '../store/graph'
import { JobsView } from './JobsView'

const job = (overrides = {}) => ({
  id: 'history-1', runId: 'run-1', jobType: 'run' as const, status: 'failed',
  canvasId: 'canvas-1', canvasName: 'Alpha research', targetNodeId: 'write-1',
  nodeLabel: 'Publish observations', backend: 'local', placement: 'local' as const,
  attempt: 'run-1', rows: 12, ms: 240, error: 'destination unavailable',
  outputs: [], createdAt: '2026-07-16T12:00:00Z', ...overrides,
})

const openAdvancedFilters = () => fireEvent.click(screen.getByText('Advanced filters'))
const openTechnicalEvidence = () => fireEvent.click(screen.getByText('Technical evidence'))

describe('JobsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.workspaceJobs.mockResolvedValue({ items: [job()], hasMore: false, nextCursor: null })
    mocks.executionManifest.mockResolvedValue({ availability: 'not_recorded', document: null })
    mocks.cancelRun.mockResolvedValue(undefined)
    mocks.retryRun.mockResolvedValue(undefined)
    mocks.cancelMergeColumnsTask.mockResolvedValue(undefined)
    mocks.retryMergeColumnsTask.mockResolvedValue(undefined)
    mocks.cancelManagedSidecarMergeTask.mockResolvedValue(undefined)
    mocks.retryManagedSidecarMergeTask.mockResolvedValue(undefined)
    mocks.listCanvases.mockResolvedValue([])
    useStore.setState({ view: 'jobs', jobsQuery: '', files: [], toasts: [] } as never)
  })

  it('distinguishes loading from an empty filtered result', async () => {
    let finish: ((value: { items: never[]; hasMore: boolean; nextCursor: null }) => void) | undefined
    mocks.workspaceJobs.mockReturnValue(new Promise((resolve) => { finish = resolve }))
    useStore.setState({ jobsQuery: 'status=failed' } as never)
    render(<JobsView />)
    expect(screen.getByText('Loading Jobs…')).toBeVisible()
    await act(async () => { finish?.({ items: [], hasMore: false, nextCursor: null }) })
    expect(screen.getByText('No Jobs match these filters.')).toBeVisible()
  })

  it('shows normalized workspace history and stable canvas/node links', async () => {
    render(<JobsView />)

    expect(await screen.findByText('Alpha research')).toBeVisible()
    expect(screen.getByText('Publish observations')).toBeVisible()
    expect(screen.getByText('Open for failure details')).toBeVisible()
    expect(screen.queryByText('destination unavailable')).toBeNull()
    expect(screen.getByText(new Date('2026-07-16T12:00:00Z').toLocaleString())).toHaveClass('whitespace-nowrap')
    fireEvent.click(screen.getByRole('button', { name: 'Open run run-1 in Alpha research', expanded: false }))
    expect(screen.getByRole('alert')).toHaveTextContent('destination unavailable')
    expect(screen.getAllByText('destination unavailable')).toHaveLength(1)
    expect(screen.getByText('Progress:').closest('div')).toHaveTextContent('Progress: Unavailable')
    expect(screen.getByText('Last durable update:').closest('div')).toHaveTextContent('Last durable update: Unavailable')
    expect(screen.getByRole('link', { name: 'Open canvas' })).toHaveAttribute('href', '#/canvas/canvas-1')
    expect(screen.getByRole('link', { name: 'Open node' })).toHaveAttribute('href', '#/canvas/canvas-1?node=write-1')
    expect(useStore.getState().jobsQuery).toContain('run=run-1')
  })

  it('keeps the differentiating suffix and full tooltip for long shared-prefix canvas names', async () => {
    const prefix = 'Robotics preprocessing experiment with shared discovery and filtering context — '
    const left = `${prefix}left-camera-pass`
    const right = `${prefix}right-camera-pass`
    const unicodePrefix = '多语言研究画布😀共享前缀数据清洗实验阶段一二三四五六七八九十—'
    const unicodeLeft = `${unicodePrefix}左侧相机`
    const unicodeRight = `${unicodePrefix}右侧相机`
    const combining = `${'a'.repeat(10)}e\u0301${'x'.repeat(17)}`
    const zwj = `${'a'.repeat(10)}👩🏽‍🔬${'x'.repeat(17)}`
    mocks.workspaceJobs.mockResolvedValue({
      items: [
        job({ id: 'history-left', runId: 'run-left', canvasName: left }),
        job({ id: 'history-right', runId: 'run-right', canvasName: right }),
        job({ id: 'history-unicode-left', runId: 'run-unicode-left', canvasName: unicodeLeft }),
        job({ id: 'history-unicode-right', runId: 'run-unicode-right', canvasName: unicodeRight }),
        job({ id: 'history-combining', runId: 'run-combining', canvasName: combining }),
        job({ id: 'history-zwj', runId: 'run-zwj', canvasName: zwj }),
      ],
      hasMore: false, nextCursor: null,
    })

    render(<JobsView />)

    const leftSubject = await screen.findByTitle(left)
    const rightSubject = await screen.findByTitle(right)
    const unicodeLeftSubject = await screen.findByTitle(unicodeLeft)
    const unicodeRightSubject = await screen.findByTitle(unicodeRight)
    const combiningSubject = await screen.findByTitle(combining)
    const zwjSubject = await screen.findByTitle(zwj)
    expect(leftSubject).toHaveTextContent(left)
    expect(rightSubject).toHaveTextContent(right)
    expect(leftSubject.lastElementChild?.textContent).not.toBe(rightSubject.lastElementChild?.textContent)
    expect(leftSubject.lastElementChild?.textContent).toContain('left-camera-pass')
    expect(rightSubject.lastElementChild?.textContent).toContain('right-camera-pass')
    expect(Array.from(unicodeLeft)).toHaveLength(35)
    expect(unicodeLeftSubject.lastElementChild?.textContent).toContain('左侧相机')
    expect(unicodeRightSubject.lastElementChild?.textContent).toContain('右侧相机')
    expect(unicodeLeftSubject.lastElementChild?.textContent)
      .not.toBe(unicodeRightSubject.lastElementChild?.textContent)
    expect(Array.from(combining)).toHaveLength(29)
    expect(combiningSubject.firstElementChild).toHaveTextContent('a'.repeat(10))
    expect(combiningSubject.lastElementChild).toHaveTextContent(`e\u0301${'x'.repeat(17)}`)
    expect(Array.from(zwj)).toHaveLength(31)
    expect(zwjSubject.firstElementChild).toHaveTextContent('a'.repeat(10))
    expect(zwjSubject.lastElementChild).toHaveTextContent(`👩🏽‍🔬${'x'.repeat(17)}`)
  })

  it('uses the history identity when a legacy row has no logical run id', async () => {
    mocks.workspaceJobs.mockResolvedValue({
      items: [job({ runId: null })], hasMore: false, nextCursor: null,
    })
    render(<JobsView />)

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open run history-1 in Alpha research', expanded: false,
    }))
    expect(screen.getByRole('alert')).toHaveTextContent('destination unavailable')
    expect(useStore.getState().jobsQuery).toContain('run=history-1')
  })

  it('keeps filters in the route and passes them to the bounded API', async () => {
    render(<JobsView />)
    await screen.findByText('Alpha research')
    openAdvancedFilters()
    fireEvent.change(screen.getByLabelText('Filter jobs by status'), { target: { value: 'running' } })
    await waitFor(() => expect(useStore.getState().jobsQuery).toBe('status=running'))
    await waitFor(() => expect(mocks.workspaceJobs).toHaveBeenLastCalledWith(expect.objectContaining({
      limit: 50, status: 'running',
    })))
  })

  it('maps quick views to existing status and time query fields', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(new Date('2026-07-21T12:00:00Z').getTime())
    try {
      render(<JobsView />)
      await screen.findByText('Alpha research')

      fireEvent.click(screen.getByRole('button', { name: 'Queued' }))
      await waitFor(() => expect(useStore.getState().jobsQuery).toBe('status=queued'))
      expect(mocks.workspaceJobs).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'queued' }))

      fireEvent.click(screen.getByRole('button', { name: 'Recent' }))
      await waitFor(() => expect(useStore.getState().jobsQuery).toBe('after=2026-07-14T12%3A00%3A00.000Z'))
      expect(mocks.workspaceJobs).toHaveBeenLastCalledWith(expect.objectContaining({ after: '2026-07-14T12:00:00.000Z', status: undefined }))

      fireEvent.click(screen.getByRole('button', { name: 'All' }))
      await waitFor(() => expect(useStore.getState().jobsQuery).toBe(''))
    } finally {
      now.mockRestore()
    }
  })

  it('keeps exact evidence closed until a researcher asks for it', async () => {
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open run run-1 in Alpha research', expanded: false }))

    expect(screen.getByRole('alert')).toHaveTextContent('destination unavailable')
    const evidence = screen.getByText('Technical evidence').closest('details')!
    expect(evidence).not.toHaveAttribute('open')
    expect(screen.getByText('Current attempt:')).not.toBeVisible()

    openTechnicalEvidence()
    expect(screen.getByText('Current attempt:')).toBeVisible()
    expect(screen.getByText('Run:').closest('div')).toHaveTextContent('run-1')
  })

  it('rechecks the same unavailable deep link after returning to Jobs', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [], hasMore: false, nextCursor: null })
    const deepLink = 'status=failed&canvas=canvas-1&run=missing-run&output=write-1%3Aout'
    useStore.setState({ jobsQuery: deepLink } as never)
    render(<JobsView />)

    expect(await screen.findByText('This Job is unavailable or you no longer have access.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Back to Jobs' }))
    expect(useStore.getState().jobsQuery).toBe('status=failed&canvas=canvas-1')
    expect(screen.queryByText('This Job is unavailable or you no longer have access.')).not.toBeInTheDocument()

    useStore.setState({ jobsQuery: deepLink } as never)
    expect(await screen.findByText('This Job is unavailable or you no longer have access.')).toBeVisible()
    expect(mocks.workspaceJobs).toHaveBeenCalledTimes(3)
  })

  it('uses authorized canvas names and current-page node/backend context while retaining canonical IDs', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [
      job({ canvasId: 'canvas-1', canvasName: 'Research', targetNodeId: 'publish', nodeLabel: 'Publish' }),
      job({ id: 'history-2', runId: 'run-2', canvasId: 'canvas-2', canvasName: 'Research', targetNodeId: 'publish', nodeLabel: 'Publish', backend: 'ray' }),
      job({ id: 'history-3', runId: 'run-3', canvasId: 'canvas-2', canvasName: 'Research', targetNodeId: 'unlabelled', nodeLabel: null, backend: 'ray' }),
    ], hasMore: false, nextCursor: null })
    useStore.setState({ files: [
      { id: 'canvas-1', name: 'Research', version: 1 },
      { id: 'canvas-2', name: 'Research', version: 1 },
    ] } as never)
    render(<JobsView />)

    await screen.findAllByText('Research')
    openAdvancedFilters()
    expect(screen.getByRole('option', { name: 'Research · canvas-1' })).toBeVisible()
    expect(screen.getByRole('option', { name: 'Research · canvas-2' })).toBeVisible()
    expect(screen.getByRole('option', { name: 'Publish · Research (canvas-1) · publish' })).toBeVisible()
    expect(screen.getByRole('option', { name: 'Node unlabelled · Research (canvas-2) · unlabelled' })).toBeVisible()

    fireEvent.change(screen.getByLabelText('Filter jobs by node'), {
      target: { value: JSON.stringify(['canvas-2', 'publish']) },
    })
    await waitFor(() => expect(useStore.getState().jobsQuery).toBe('canvas=canvas-2&node=publish'))
    expect(mocks.workspaceJobs).toHaveBeenLastCalledWith(expect.objectContaining({
      canvasId: 'canvas-2', nodeId: 'publish', limit: 50,
    }))

    fireEvent.change(screen.getByLabelText('Filter jobs by canvas'), { target: { value: 'canvas-1' } })
    await waitFor(() => expect(useStore.getState().jobsQuery).toBe('canvas=canvas-1'))
    await waitFor(() => expect(mocks.workspaceJobs).toHaveBeenLastCalledWith(expect.objectContaining({
      canvasId: 'canvas-1', nodeId: undefined, limit: 50,
    })))

    fireEvent.change(screen.getByLabelText('Filter jobs by backend'), { target: { value: 'ray' } })
    await waitFor(() => expect(useStore.getState().jobsQuery).toContain('backend=ray'))
  })

  it('keeps a deep-linked exact ID filter editable without inventing an inaccessible canvas name', async () => {
    useStore.setState({ jobsQuery: 'canvas=not-accessible&node=exact-node&backend=exact-backend' } as never)
    render(<JobsView />)

    openAdvancedFilters()
    expect(await screen.findByRole('option', { name: 'Exact canvas ID: not-accessible' })).toBeVisible()
    expect(screen.getByRole('option', { name: 'Exact node ID: exact-node' })).toBeVisible()
    expect(screen.getByLabelText('Filter jobs by node')).toHaveValue(JSON.stringify(['not-accessible', 'exact-node']))
    expect(screen.getByRole('option', { name: 'Exact backend ID: exact-backend' })).toBeVisible()
    expect(screen.getByLabelText('Filter jobs by backend')).toHaveValue('exact-backend')
    expect(screen.getByLabelText('Filter jobs by canvas id (exact)')).toHaveValue('not-accessible')
    expect(screen.getByLabelText('Filter jobs by node id (exact)')).toHaveValue('exact-node')
    expect(screen.getByLabelText('Filter jobs by backend id (exact)')).toHaveValue('exact-backend')
  })

  it('preserves completed pages when a load-more request fails', async () => {
    mocks.workspaceJobs
      .mockResolvedValueOnce({ items: [job()], hasMore: true, nextCursor: 'next-page' })
      .mockRejectedValueOnce(new Error('network unavailable'))
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))

    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn’t load more Jobs: network unavailable")
    expect(screen.getByText('Alpha research')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Retry load more' })).toBeVisible()
  })

  it('auto-refreshes an active first page and records the successful refresh', async () => {
    vi.useFakeTimers()
    mocks.workspaceJobs.mockResolvedValue({
      items: [job({ status: 'running', error: null })], hasMore: false, nextCursor: null,
    })
    try {
      render(<JobsView />)
      await act(async () => { await Promise.resolve() })
      expect(screen.getByText(/Active jobs refresh automatically · Updated/)).toBeVisible()

      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      expect(mocks.workspaceJobs).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('labels a successful first page without active jobs in user-facing terms', async () => {
    render(<JobsView />)

    expect(await screen.findByText(/No active jobs · Updated/)).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Runs and background tasks' })).toBeVisible()
  })

  it('does not treat an active direct-link result as an active first page', async () => {
    vi.useFakeTimers()
    mocks.workspaceJobs
      .mockResolvedValueOnce({ items: [job({ id: 'first-page', runId: 'first-page', status: 'done', error: null })], hasMore: false, nextCursor: null })
      .mockResolvedValueOnce({ items: [job({ id: 'direct-run', runId: 'direct-run', status: 'running', error: null })], hasMore: false, nextCursor: null })
    useStore.setState({ jobsQuery: 'run=direct-run' } as never)
    try {
      render(<JobsView />)
      await act(async () => { await Promise.resolve(); await Promise.resolve() })

      expect(screen.getByRole('button', { name: 'Open run direct-run in Alpha research', expanded: true })).toBeVisible()
      expect(screen.getByText(/No active jobs · Updated/)).toBeVisible()
      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      expect(mocks.workspaceJobs).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('pauses automatic refresh only after Load more succeeds', async () => {
    mocks.workspaceJobs
      .mockResolvedValueOnce({ items: [job({ status: 'running', error: null })], hasMore: true, nextCursor: 'next-page' })
      .mockResolvedValueOnce({ items: [job({ id: 'history-2', runId: 'run-2', status: 'done', error: null })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))

    expect(await screen.findByRole('button', { name: 'Open run run-2 in Alpha research', expanded: false })).toBeVisible()
    expect(screen.getByText(/Loaded job history · Auto-refresh paused · Updated/)).toBeVisible()
  })

  it('replaces paginated pages with a fresh first page on manual refresh', async () => {
    mocks.workspaceJobs
      .mockResolvedValueOnce({ items: [job({ status: 'running', error: null })], hasMore: true, nextCursor: 'next-page' })
      .mockResolvedValueOnce({ items: [job({ id: 'history-2', runId: 'run-2', status: 'done', error: null })], hasMore: false, nextCursor: null })
      .mockResolvedValueOnce({ items: [job({ id: 'history-3', runId: 'run-3', status: 'running', error: null })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))
    await screen.findByRole('button', { name: 'Open run run-2 in Alpha research', expanded: false })

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByRole('button', { name: 'Open run run-3 in Alpha research', expanded: false })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Open run run-2 in Alpha research', expanded: false })).not.toBeInTheDocument()
    expect(screen.getByText(/Active jobs refresh automatically · Updated/)).toBeVisible()
    expect(mocks.workspaceJobs).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: undefined, limit: 50 }))
  })

  it('retains an explicitly selected direct-link result across manual refresh', async () => {
    mocks.workspaceJobs
      .mockResolvedValueOnce({ items: [job({ id: 'first-page', runId: 'first-page', error: null })], hasMore: false, nextCursor: null })
      .mockResolvedValueOnce({ items: [job({ id: 'direct-run', runId: 'direct-run', error: null })], hasMore: false, nextCursor: null })
      .mockResolvedValueOnce({ items: [job({ id: 'refreshed-page', runId: 'refreshed-page', error: null })], hasMore: false, nextCursor: null })
      .mockResolvedValueOnce({ items: [job({ id: 'direct-run', runId: 'direct-run', error: null })], hasMore: false, nextCursor: null })
    useStore.setState({ jobsQuery: 'run=direct-run' } as never)
    render(<JobsView />)
    await screen.findByRole('button', { name: 'Open run direct-run in Alpha research', expanded: true })

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(mocks.workspaceJobs).toHaveBeenCalledTimes(4))
    expect(screen.getByRole('button', { name: 'Open run direct-run in Alpha research', expanded: true })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Open run refreshed-page in Alpha research', expanded: false })).toBeVisible()
  })

  it('keeps the last successful first page visible after refresh failure', async () => {
    mocks.workspaceJobs
      .mockResolvedValueOnce({ items: [job({ status: 'running', error: null })], hasMore: false, nextCursor: null })
      .mockRejectedValueOnce(new Error('network unavailable'))
    render(<JobsView />)
    await screen.findByRole('button', { name: 'Open run run-1 in Alpha research', expanded: false })

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Couldn’t refresh Jobs: network unavailable')
    expect(screen.getByRole('button', { name: 'Open run run-1 in Alpha research', expanded: false })).toBeVisible()
    expect(screen.getByText(/Refresh failed · Updated/)).toBeVisible()
  })

  it('deep-links and opens a retained result by run/node/port identity', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({ status: 'done', error: null, outputs: [{
      nodeId: 'write-1', portId: 'out', portLabel: 'Result', wire: 'dataset',
      publicationKind: 'result', outcome: 'committed', uri: 'file:///result.parquet', rows: 12,
    }] })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open run run-1 in Alpha research', expanded: false }))
    fireEvent.click(screen.getByRole('button', { name: 'Open result' }))

    await waitFor(() => expect(useStore.getState().jobsQuery).toContain('output=write-1%3Aout'))
    expect(screen.getByTestId('full-result')).toBeVisible()
    expect(screen.getByRole('complementary', { name: 'Retained result' })).toBeVisible()
  })

  it('keeps a completed row concise and moves multiple port identities into technical evidence', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      status: 'done', progress: 1, error: null, rows: 1,
      outputs: [
        {
          nodeId: 'transform-1', portId: 'clean', portLabel: 'Clean rows', wire: 'dataset',
          publicationKind: 'result', outcome: 'committed', uri: 'file:///clean.parquet', rows: 1,
        },
        {
          nodeId: 'transform-1', portId: 'rejected', portLabel: 'Rejected rows', wire: 'dataset',
          publicationKind: 'result', outcome: 'committed', uri: 'file:///rejected.parquet', rows: 0,
        },
      ],
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)

    const row = await screen.findByRole('button', {
      name: 'Open run run-1 in Alpha research', expanded: false,
    })
    expect(row).toHaveTextContent('done')
    expect(row).not.toHaveTextContent('100%')
    expect(row).toHaveTextContent('2 outputs available')
    expect(row.textContent?.match(/1 row/g)).toHaveLength(1)

    fireEvent.click(row)
    expect(screen.queryByText('Duration:', { exact: true })).not.toBeInTheDocument()
    expect(screen.queryByText('Rows:', { exact: true })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open result 1' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Open result 2' })).toBeVisible()
    openTechnicalEvidence()
    expect(screen.getByText('Retained results:', { exact: true }).closest('div')).toHaveTextContent(
      'Result 1 · transform-1:clean, Result 2 · transform-1:rejected',
    )
  })

  it('keeps progress visible for active work without adding a second status signal', async () => {
    mocks.workspaceJobs.mockResolvedValue({
      items: [job({ status: 'running', progress: 0.5, error: null })],
      hasMore: false, nextCursor: null,
    })
    render(<JobsView />)

    const row = await screen.findByRole('button', {
      name: 'Open run run-1 in Alpha research', expanded: false,
    })
    expect(row).toHaveTextContent('running')
    expect(row).toHaveTextContent('50%')
    expect(row.textContent?.match(/running/g)).toHaveLength(1)
    expect(row).not.toHaveTextContent('●')
  })

  it('labels ordinary Transform and Profile outputs as available while preserving row counts', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [
      job({
        id: 'transform-history', runId: 'transform-run', status: 'done', error: null,
        targetNodeId: 'transform-1', nodeLabel: 'Normalize readings', rows: 12,
        outputs: [{
          nodeId: 'transform-1', portId: 'out', portLabel: 'Result', wire: 'dataset',
          publicationKind: 'result', outcome: 'committed',
          uri: 'file:///transform-result.parquet', rows: 12,
        }],
      }),
      job({
        id: 'profile-history', runId: 'profile-run', jobType: 'profile', status: 'done',
        error: null, targetNodeId: 'profile-1', nodeLabel: 'Profile readings',
        rows: null, profile: { rowCount: 987 },
        outputs: [{
          nodeId: 'profile-1', portId: 'profile', portLabel: 'Profile', wire: 'dataset',
          publicationKind: 'result', outcome: 'committed',
          uri: 'file:///profile-result.parquet', rows: 987,
        }],
      }),
    ], hasMore: false, nextCursor: null })
    render(<JobsView />)

    const transform = (await screen.findByRole('button', {
      name: 'Open run transform-run in Alpha research', expanded: false,
    })).closest('article')
    expect(transform).toHaveTextContent('1 output available')
    expect(transform).toHaveTextContent('12 rows')
    expect(transform).not.toHaveTextContent('published')

    const profile = screen.getByRole('button', {
      name: 'Open run profile-run in Alpha research', expanded: false,
    }).closest('article')
    expect(profile).toHaveTextContent('1 output available')
    expect(profile).toHaveTextContent('987 rows')
    expect(profile).not.toHaveTextContent('published')
  })

  it('uses a managed Write receipt to label an exact dataset revision as published', async () => {
    const receipt = {
      datasetId: 'dataset-1', revisionId: 'revision-7', name: 'Published readings',
      rows: 12, bytes: 120, durable: true as const,
      head: { datasetId: 'dataset-1', revisionId: 'revision-7', retentionOwner: 'core' },
      schema: [], partitions: [],
      publication: {
        provider: 'managed-local-file', logicalUri: 'managed://dataset-1',
        artifactUri: 'file:///managed/revision-7.parquet', publishSequence: 1,
        idempotencyKey: 'write-run',
      },
    }
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      id: 'write-history', runId: 'write-run', taskId: 'write-run',
      status: 'done', error: null, targetNodeId: 'write-1', nodeLabel: 'Store readings',
      outputs: [{
        nodeId: 'write-1', portId: 'out', portLabel: 'Dataset', wire: 'dataset',
        publicationKind: 'catalog', outcome: 'committed',
        uri: receipt.publication.artifactUri, table: receipt.name, rows: 12,
        writeReceipt: receipt,
      }],
      outputReceipt: receipt,
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)

    const row = await screen.findByRole('button', {
      name: 'Open run write-run in Alpha research', expanded: false,
    })
    expect(row.closest('article')).toHaveTextContent('Dataset revision published')
    expect(row.closest('article')).toHaveTextContent('12 rows')
    expect(row.closest('article')).not.toHaveTextContent('output retained')
    fireEvent.click(row)
    expect(screen.getByRole('link', { name: 'Open dataset' })).toHaveAttribute(
      'href',
      '#/workspace/dataset%3Adataset-1?scope=datasets&revision=revision-7&revisionDataset=dataset-1&returnView=jobs&returnQuery=run%3Dwrite-run',
    )
    expect(screen.queryByRole('button', { name: 'Open result' })).not.toBeInTheDocument()
    openTechnicalEvidence()
    expect(screen.getByText('Receipt:', { exact: true }).closest('div')).toHaveTextContent(
      'dataset dataset-1 · revision revision-7 · 12 rows',
    )
    expect(screen.getAllByRole('link', { name: 'Open dataset' })).toHaveLength(1)
  })

  it('shows exact durable task state and requests cancellation from Jobs', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      runId: 'task-1', taskId: 'task-1', status: 'running', error: null,
      progress: 0.5, updatedAt: '2026-07-16T12:00:30Z',
      inputManifest: [{ node_id: 'source', dataset_id: 'dataset-1', revision_id: 'revision-7', provider: 'lance', resolved_at: '2026-07-16T12:00:00Z' }],
      taskAttempts: [{ id: 'attempt-1', attemptNumber: 1, status: 'running', progress: 0.5, error: null, startedAt: '2026-07-16T12:00:00Z', completedAt: null, updatedAt: '2026-07-16T12:00:30Z' }],
      cancelRequested: false, canRetry: false,
      writeIntent: { mode: 'replace', destination: { name: 'durable', logicalUri: 'managed://durable', provider: 'managed-local-file' }, expectedHead: { revisionId: 'head-6' } },
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', {
      name: 'Open run task-1 in Alpha research', expanded: false,
    }))

    openTechnicalEvidence()
    expect(screen.getByText(/dataset-1@revision-7/)).toBeVisible()
    expect(screen.getByText(/#1 running/).closest('li')).toHaveTextContent('Progress 50%')
    expect(screen.getByText('Progress:').closest('div')).toHaveTextContent('Progress: 50%')
    expect(screen.getByText('Last durable update:').closest('div')).not.toHaveTextContent('Unavailable')
    expect(screen.getByText(/replace · durable · expected head head-6/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }))
    await waitFor(() => expect(mocks.cancelRun).toHaveBeenCalledWith('task-1'))
  })

  it('uses only the dedicated merge task actions and never falls back from an exact receipt', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      runId: 'merge-1', taskId: 'merge-1', status: 'running', error: null, canCancel: true,
      mergeColumns: { producerKind: 'sparse-output', phase: 'merging', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-1', candidate: 'pending', reused: false, canRetry: false, canCancel: true },
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open run merge-1 in Alpha research', expanded: false }))
    openTechnicalEvidence()
    expect(screen.getByText('Column merge:', { exact: true })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }))
    await waitFor(() => expect(mocks.cancelMergeColumnsTask).toHaveBeenCalledWith('merge-1'))
    expect(mocks.cancelRun).not.toHaveBeenCalledWith('merge-1')
  })

  it('uses the server-projected producer kind for managed-sidecar task actions', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      runId: 'sidecar-1', taskId: 'sidecar-1', status: 'running', error: null, canCancel: true,
      mergeColumns: { producerKind: 'managed-sidecar', phase: 'merging', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-1', candidate: 'pending', reused: false, canRetry: false, canCancel: true },
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open run sidecar-1 in Alpha research', expanded: false }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }))
    await waitFor(() => expect(mocks.cancelManagedSidecarMergeTask).toHaveBeenCalledWith('sidecar-1'))
    expect(mocks.cancelMergeColumnsTask).not.toHaveBeenCalledWith('sidecar-1')
  })

  it('uses the managed-sidecar retry endpoint for a failed managed task', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      runId: 'sidecar-retry', taskId: 'sidecar-retry', status: 'failed', error: 'worker lost', canRetry: true,
      mergeColumns: { producerKind: 'managed-sidecar', phase: 'failed', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-1', candidate: 'pending', reused: false, canRetry: true, canCancel: false },
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open run sidecar-retry in Alpha research', expanded: false }))
    fireEvent.click(screen.getByRole('button', { name: 'Retry task' }))
    await waitFor(() => expect(mocks.retryManagedSidecarMergeTask).toHaveBeenCalledWith('sidecar-retry', expect.any(String)))
    expect(mocks.retryMergeColumnsTask).not.toHaveBeenCalledWith('sidecar-retry')
  })

  it('routes a stale merge Task back to its real Write node for explicit re-admission', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      runId: 'merge-stale', taskId: 'merge-stale', targetNodeId: 'write-merge', status: 'failed',
      error: 'stale_expected_head', canCancel: false, canRetry: false,
      mergeColumns: { producerKind: 'sparse-output', phase: 'failed', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-1', candidate: 'committed', reused: false, canRetry: false, canCancel: false, diagnosticCode: 'stale_expected_head' },
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open run merge-stale in Alpha research', expanded: false }))

    expect(screen.getByRole('link', { name: 'Re-admit in Canvas' })).toHaveAttribute('href', '#/canvas/canvas-1?node=write-merge')
    expect(screen.queryByRole('link', { name: 'Open node' })).not.toBeInTheDocument()
  })

  it('routes an exact merge receipt to the immutable shared dataset viewer', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      runId: 'merge-done', taskId: 'merge-done', status: 'done', error: null,
      outputReceipt: { datasetId: 'dataset-1', revisionId: 'rev-gone', rows: 2, bytes: 12, durable: true, head: { datasetId: 'dataset-1', revisionId: 'rev-gone', retentionOwner: 'core' }, schema: [], partitions: [], publication: { provider: 'managed-local-file', logicalUri: 'managed://dataset-1', artifactUri: 'redacted', publishSequence: 1, idempotencyKey: 'merge-done' } },
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open run merge-done in Alpha research', expanded: false }))
    openTechnicalEvidence()
    expect(screen.getByRole('link', { name: 'Open dataset' })).toHaveAttribute(
      'href',
      '#/workspace/dataset%3Adataset-1?scope=datasets&revision=rev-gone&revisionDataset=dataset-1&returnView=jobs&returnQuery=run%3Dmerge-done',
    )
    expect(mocks.workspaceJobs).toHaveBeenCalledTimes(1)
  })

  it('loads the selected durable task manifest through its current Canvas subject', async () => {
    const digest = 'b'.repeat(64)
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      id: 't:task-manifest', runId: 'task-manifest', taskId: 'task-manifest', error: null,
      executionManifestSha256: digest, executionManifestSchemaVersion: 1,
      executionManifestAvailability: 'available', executionManifestReconstructable: true,
    })], hasMore: false, nextCursor: null })
    mocks.executionManifest.mockResolvedValue({
      sha256: digest, schemaVersion: 1, availability: 'available', document: {
        schemaVersion: 1,
        graph: { nodes: [], edges: [], requirements: [] },
        target: { nodeId: 'write-1', portId: null },
        admittedInputs: [], writeIntent: null,
        descriptors: { core: { apiVersion: '1' }, nodes: [], plugins: [] },
        parameters: [{ name: 'threshold', type: 'integer', value: 10 }],
      },
    })
    render(<JobsView />)

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open run task-manifest in Alpha research', expanded: false,
    }))
    openTechnicalEvidence()
    expect(mocks.executionManifest).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /Execution manifest/ }))

    expect(await screen.findByText('Submitted graph')).toBeVisible()
    expect(screen.getByText(/"threshold"/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Clone as new Canvas…' })).toBeVisible()
    expect(mocks.executionManifest).toHaveBeenCalledWith('canvas-1', 't:task-manifest')
  })

  it('reuses one retry action id after an ambiguous request failure', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      runId: 'task-2', taskId: 'task-2', status: 'failed', canRetry: true,
      taskAttempts: [
        { id: 'attempt-1', attemptNumber: 1, status: 'fenced', progress: null, error: 'worker lost', startedAt: null, completedAt: '2026-07-16T12:01:00Z', updatedAt: '2026-07-16T12:01:00Z' },
        { id: 'attempt-2', attemptNumber: 2, status: 'failed', progress: null, error: 'worker lost', startedAt: null, completedAt: '2026-07-16T12:02:00Z', updatedAt: '2026-07-16T12:02:00Z' },
      ],
    })], hasMore: false, nextCursor: null })
    mocks.retryRun.mockRejectedValueOnce(new Error('response lost')).mockResolvedValueOnce(undefined)
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', {
      name: 'Open run task-2 in Alpha research', expanded: false,
    }))
    expect(screen.getByText(/#1 fenced/).closest('li')).toHaveTextContent('Progress Unavailable')
    expect(screen.getByText(/#2 failed/).closest('li')).toHaveTextContent('Progress Unavailable')
    fireEvent.click(screen.getByRole('button', { name: 'Retry task' }))
    expect(await screen.findByText(/Job action failed: response lost/)).toBeVisible()
    const actionId = mocks.retryRun.mock.calls[0][1]
    fireEvent.click(screen.getByRole('button', { name: 'Retry task' }))
    await waitFor(() => expect(mocks.retryRun).toHaveBeenCalledTimes(2))
    expect(mocks.retryRun.mock.calls[1]).toEqual(['task-2', actionId])
  })

  it('renders parent-only bounded fan-out stage and partition progress', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      runId: 'fan-1', taskId: 'fan-1', status: 'running', error: null,
      boundedFanout: {
        stage: 'running_partitions',
        partitionCount: 4,
        completedPartitions: 2,
        failedPartitions: 0,
        checkpoint: 'reused',
        gather: 'pending',
        diagnosticCode: null,
      },
      canCancel: true,
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', {
      name: 'Open run fan-1 in Alpha research', expanded: false,
    }))
    const fanout = screen.getByText('Fan-out:').closest('div')
    expect(screen.getByText('Phase:').closest('div')).toHaveTextContent('Phase: Fan-out · running partitions')
    expect(fanout).toHaveTextContent('Fan-out: 2/4 partitions · checkpoint reused · gather pending')
    expect(screen.queryByText(/unitId|planDigest|range/i)).not.toBeInTheDocument()
  })

  it('renders existing external-wait and checkpoint phases without inventing a generic phase', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [
      job({
        id: 'external-history', runId: 'external-1', taskId: 'external-1', status: 'running',
        externalWait: { providerKind: 'fixture-local', phase: 'downloading', attemptNumber: 2, cancelRequested: false, canRetry: false },
        taskAttempts: [{ id: 'external-attempt', attemptNumber: 2, status: 'running', progress: null, updatedAt: '2026-07-16T12:03:00Z' }],
      }),
      job({
        id: 'checkpoint-history', runId: 'checkpoint-1', taskId: 'checkpoint-1', status: 'running',
        checkpoint: { phase: 'materializing', checkpointNodeId: 'checkpoint', outputPortId: 'out', resumeEligible: false, clientKey: 'checkpoint:checkpoint-1' },
        taskAttempts: [{ id: 'checkpoint-attempt', attemptNumber: 1, status: 'running', progress: null, updatedAt: '2026-07-16T12:04:00Z' }],
      }),
    ], hasMore: false, nextCursor: null })
    render(<JobsView />)

    fireEvent.click(await screen.findByRole('button', { name: 'Open run external-1 in Alpha research', expanded: false }))
    expect(screen.getByText('Phase:').closest('div')).toHaveTextContent('Phase: External wait · downloading')
    expect(screen.getByText('External provider:').closest('div')).toHaveTextContent('fixture-local · provider attempt #2')

    fireEvent.click(screen.getByRole('button', { name: 'Open run checkpoint-1 in Alpha research', expanded: false }))
    expect(screen.getByText('Phase:').closest('div')).toHaveTextContent('Phase: Checkpoint · materializing')
    expect(screen.getByText('Checkpoint:').closest('div')).toHaveTextContent('checkpoint:out')
  })

  it('renders a canvas-less dataset task with an exact dataset-viewer deep-link', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      id: 't:restore-1', runId: 'restore-1', status: 'done', canvasId: null, canvasName: null,
      taskId: 'restore-1', nodeLabel: 'Climate observations', error: null,
      datasetContext: { taskKind: 'restore_revision_write', datasetId: 'ds-logical-9', revisionId: 'rev-9', name: 'Climate observations' },
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)

    expect(await screen.findByText('Dataset restore · Climate observations')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open run restore-1 in Dataset restore · Climate observations', expanded: false }))
    const link = screen.getByRole('link', { name: 'Open dataset' })
    expect(link).toHaveAttribute(
      'href',
      '#/workspace/dataset%3Ads-logical-9?scope=datasets&revision=rev-9&revisionDataset=ds-logical-9&returnView=jobs&returnQuery=run%3Drestore-1',
    )
  })

})
