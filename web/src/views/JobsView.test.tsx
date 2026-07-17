import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  workspaceJobs: vi.fn(), cancelRun: vi.fn(), retryRun: vi.fn(),
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
    useStore.setState({ view: 'jobs', jobsQuery: '', toasts: [] } as never)
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
      inputManifest: [{ node_id: 'source', dataset_id: 'dataset-1', revision_id: 'revision-7', provider: 'lance', resolved_at: '2026-07-16T12:00:00Z' }],
      taskAttempts: [{ id: 'attempt-1', attemptNumber: 1, status: 'running', progress: 0.5, error: null, startedAt: '2026-07-16T12:00:00Z', completedAt: null }],
      cancelRequested: false, canRetry: false,
      writeIntent: { mode: 'replace', destination: { name: 'durable', logicalUri: 'managed://durable', provider: 'managed-local-file' }, expectedHead: { revisionId: 'head-6' } },
    })], hasMore: false, nextCursor: null })
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', {
      name: 'Open run task-1 in Alpha research', expanded: false,
    }))

    expect(screen.getByText(/dataset-1@revision-7/)).toBeVisible()
    expect(screen.getByText(/#1 running/)).toBeVisible()
    expect(screen.getByText(/replace · durable · expected head head-6/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }))
    await waitFor(() => expect(mocks.cancelRun).toHaveBeenCalledWith('task-1'))
  })

  it('reuses one retry action id after an ambiguous request failure', async () => {
    mocks.workspaceJobs.mockResolvedValue({ items: [job({
      runId: 'task-2', taskId: 'task-2', status: 'failed', canRetry: true,
      taskAttempts: [{ id: 'attempt-1', attemptNumber: 1, status: 'failed', progress: null, error: 'worker lost', startedAt: null, completedAt: '2026-07-16T12:01:00Z' }],
    })], hasMore: false, nextCursor: null })
    mocks.retryRun.mockRejectedValueOnce(new Error('response lost')).mockResolvedValueOnce(undefined)
    render(<JobsView />)
    fireEvent.click(await screen.findByRole('button', {
      name: 'Open run task-2 in Alpha research', expanded: false,
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Retry task' }))
    expect(await screen.findByText(/Job action failed: response lost/)).toBeVisible()
    const actionId = mocks.retryRun.mock.calls[0][1]
    fireEvent.click(screen.getByRole('button', { name: 'Retry task' }))
    await waitFor(() => expect(mocks.retryRun).toHaveBeenCalledTimes(2))
    expect(mocks.retryRun.mock.calls[1]).toEqual(['task-2', actionId])
  })
})
