import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  workspaceJobs: vi.fn(), cancelRun: vi.fn(), retryRun: vi.fn(), listCanvases: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../panels/DataPanel', () => ({ FullResult: () => <div data-testid="full-result">artifact</div> }))

import { useStore } from '../store/graph'
import { JobsView } from './JobsView'

const job = (overrides = {}) => ({
  id: 'history-1', runId: 'run-1', jobType: 'run' as const, status: 'failed',
  canvasId: 'canvas-1', canvasName: 'Alpha research', targetNodeId: 'write-1',
  nodeLabel: 'Publish observations', backend: 'local', placement: 'local' as const,
  attempt: 'run-1', rows: 12, ms: 240, error: 'destination unavailable',
  outputs: [], createdAt: '2026-07-16T12:00:00Z', ...overrides,
})

describe('JobsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.workspaceJobs.mockResolvedValue({ items: [job()], hasMore: false, nextCursor: null })
    mocks.cancelRun.mockResolvedValue(undefined)
    mocks.retryRun.mockResolvedValue(undefined)
    mocks.listCanvases.mockResolvedValue([])
    useStore.setState({ view: 'jobs', jobsQuery: '', files: [], toasts: [] } as never)
  })

  it('distinguishes loading from an empty filtered result', async () => {
    let finish: ((value: { items: never[]; hasMore: boolean; nextCursor: null }) => void) | undefined
    mocks.workspaceJobs.mockReturnValue(new Promise((resolve) => { finish = resolve }))
    render(<JobsView />)
    expect(screen.getByText('Loading Jobs…')).toBeVisible()
    finish?.({ items: [], hasMore: false, nextCursor: null })
    expect(await screen.findByText('No runs match these filters.')).toBeVisible()
  })

  it('shows normalized workspace history and stable canvas/node links', async () => {
    render(<JobsView />)

    expect(await screen.findByText('Alpha research')).toBeVisible()
    expect(screen.getByText('Publish observations')).toBeVisible()
    expect(screen.queryByText('destination unavailable')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open run run-1 in Alpha research', expanded: false }))
    expect(screen.getByRole('alert')).toHaveTextContent('destination unavailable')
    expect(screen.getByText('Progress:').closest('div')).toHaveTextContent('Progress: Unavailable')
    expect(screen.getByText('Last durable update:').closest('div')).toHaveTextContent('Last durable update: Unavailable')
    expect(screen.getByRole('link', { name: 'Open canvas' })).toHaveAttribute('href', '#/canvas/canvas-1')
    expect(screen.getByRole('link', { name: 'Open node' })).toHaveAttribute('href', '#/canvas/canvas-1?node=write-1')
    expect(useStore.getState().jobsQuery).toContain('run=run-1')
  })

  it('uses the history identity when a legacy row has no logical run id', async () => {
    mocks.workspaceJobs.mockResolvedValue({
      items: [job({ runId: null })], hasMore: false, nextCursor: null,
    })
    render(<JobsView />)

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open run history-1 in Alpha research', expanded: false,
    }))
    expect(screen.getByText('destination unavailable')).toBeVisible()
    expect(useStore.getState().jobsQuery).toContain('run=history-1')
  })

  it('keeps filters in the route and passes them to the bounded API', async () => {
    render(<JobsView />)
    await screen.findByText('Alpha research')
    fireEvent.change(screen.getByLabelText('Filter jobs by status'), { target: { value: 'running' } })
    await waitFor(() => expect(useStore.getState().jobsQuery).toBe('status=running'))
    await waitFor(() => expect(mocks.workspaceJobs).toHaveBeenLastCalledWith(expect.objectContaining({
      limit: 50, status: 'running',
    })))
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

    expect(await screen.findByRole('option', { name: 'Exact canvas ID: not-accessible' })).toBeVisible()
    expect(screen.getByRole('option', { name: 'Exact node ID: exact-node' })).toBeVisible()
    expect(screen.getByLabelText('Filter jobs by node')).toHaveValue(JSON.stringify(['not-accessible', 'exact-node']))
    expect(screen.getByRole('option', { name: 'Exact backend ID: exact-backend' })).toBeVisible()
    expect(screen.getByLabelText('Filter jobs by backend')).toHaveValue('exact-backend')
    fireEvent.click(screen.getByText('Advanced exact IDs'))
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
      expect(screen.getByText(/Live first page\. Last successful refresh:/)).toBeVisible()

      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      expect(mocks.workspaceJobs).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('labels a successful first page without active Jobs as a snapshot', async () => {
    render(<JobsView />)

    expect(await screen.findByText(/Snapshot; no active Jobs\. Last successful refresh:/)).toBeVisible()
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
      expect(screen.getByText(/Snapshot; no active Jobs\. Last successful refresh:/)).toBeVisible()
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
    expect(screen.getByText(/Automatic refresh paused after loading more\. Last successful refresh:/)).toBeVisible()
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
    expect(screen.getByText(/Live first page\. Last successful refresh:/)).toBeVisible()
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
    expect(screen.getByText(/Refresh failed; showing the last successful first page\. Last successful refresh:/)).toBeVisible()
  })

  it('deep-links and opens a retained artifact by run/node/port identity', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({ status: 'done', error: null, outputs: [{
      nodeId: 'write-1', portId: 'out', portLabel: 'Result', wire: 'dataset',
      publicationKind: 'result', outcome: 'committed', uri: 'file:///result.parquet', rows: 12,
    }] })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open run run-1 in Alpha research', expanded: false }))
    fireEvent.click(screen.getByRole('button', { name: 'Open Result' }))

    await waitFor(() => expect(useStore.getState().jobsQuery).toContain('output=write-1%3Aout'))
    expect(screen.getByTestId('full-result')).toBeVisible()
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

    expect(screen.getByText(/dataset-1@revision-7/)).toBeVisible()
    expect(screen.getByText(/#1 running/).closest('li')).toHaveTextContent('Progress 50%')
    expect(screen.getByText('Progress:').closest('div')).toHaveTextContent('Progress: 50%')
    expect(screen.getByText('Last durable update:').closest('div')).not.toHaveTextContent('Unavailable')
    expect(screen.getByText(/replace · durable · expected head head-6/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }))
    await waitFor(() => expect(mocks.cancelRun).toHaveBeenCalledWith('task-1'))
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
})
