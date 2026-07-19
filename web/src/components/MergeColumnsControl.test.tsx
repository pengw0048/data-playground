import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  state: {} as any,
  preflight: vi.fn(), submit: vi.fn(), task: vi.fn(), jobs: vi.fn(), revision: vi.fn(),
  tableByRegistration: vi.fn(), resolveDatasetRevision: vi.fn(), cancel: vi.fn(), retry: vi.fn(),
}))

vi.mock('../store/graph', () => ({
  roleCanEdit: () => true,
  useStore: (selector: (state: any) => unknown) => selector(mocks.state),
}))
vi.mock('../api/client', () => ({
  api: {
    mergeColumnsPreflight: mocks.preflight, submitMergeColumns: mocks.submit,
    mergeColumnsTask: mocks.task, workspaceJobs: mocks.jobs, datasetRevision: mocks.revision,
    tableByRegistration: mocks.tableByRegistration, resolveDatasetRevision: mocks.resolveDatasetRevision,
    cancelMergeColumnsTask: mocks.cancel, retryMergeColumnsTask: mocks.retry,
  },
  toMergeColumnsGraph: (doc: any, writeId: string) => ({ id: doc.id, version: doc.version, requirements: [], parameters: [], nodes: doc.nodes.filter((node: any) => ['source', 'select', writeId].includes(node.id)), edges: doc.edges }),
  KernelError: class KernelError extends Error {},
}))

import { MergeColumnsControl } from './MergeColumnsControl'

const preflight = {
  base: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'rev-1' }, declaredKey: ['id'], identityColumns: ['id'],
  coverage: { base: { rows: 2, uniqueIdentities: 2, nullRows: 0, duplicateGroups: 0, duplicateRows: 0 }, candidate: { rows: 2, uniqueIdentities: 2, nullRows: 0, duplicateGroups: 0, duplicateRows: 0 }, matchedIdentities: 2, missingIdentities: 0, extraIdentities: 0, status: 'complete' },
  rules: [{ source: 'score', target: 'score', mode: 'add' as const }], expectedHead: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'rev-1' }, outputSchema: [],
  provenance: { producer: 'source_to_select', source: 'exact', selectKind: 'builtin', selectVersion: 1 }, eligible: true,
}

describe('MergeColumnsControl', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.state = {
      canvasRole: 'owner', jobsQuery: '', setJobsQuery: vi.fn(),
      doc: { id: 'canvas-1', version: 3, requirements: [], parameters: [], nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', config: { uri: 'exact.parquet', datasetRef: { kind: 'exact', datasetId: 'dataset-1', revisionId: 'rev-1' } } } },
        { id: 'select', type: 'select', position: { x: 1, y: 0 }, data: { title: 'select', config: { select: 'id, score' } } },
        { id: 'write', type: 'write', position: { x: 2, y: 0 }, data: { title: 'write', config: { filename: 'exact.parquet', mergeColumns: { submissionId: 'submission-1', identityColumns: ['id'], rules: [{ source: 'score', target: 'score', mode: 'add' }] } } } },
      ], edges: [{ id: 'a', source: 'source', target: 'select' }, { id: 'b', source: 'select', target: 'write' }] },
      updateConfig: vi.fn((id: string, patch: any) => {
        const node = mocks.state.doc.nodes.find((item: any) => item.id === id)
        node.data.config = { ...node.data.config, ...patch }
      }),
    }
    mocks.preflight.mockResolvedValue(preflight)
    mocks.submit.mockResolvedValue({ taskId: 'task-1', status: 'queued', canRetry: false, canCancel: true, mergeColumns: { phase: 'validating', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-1', candidate: 'pending', reused: false, canRetry: false, canCancel: true } })
    mocks.tableByRegistration.mockResolvedValue({ id: 'dataset-1', uri: 'managed://dataset-1/current.parquet' })
    mocks.resolveDatasetRevision.mockResolvedValue({ datasetId: 'dataset-1', revisionId: 'rev-current', committedAt: '2026-07-19T12:00:00Z', retentionOwner: 'core', selector: 'latest' })
  })

  it('requires authoritative current preflight before submitting the bounded graph', async () => {
    render(<MergeColumnsControl nodeId="write" />)
    const run = screen.getByRole('button', { name: 'Run column merge' })
    expect(run).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    await screen.findByText('Eligible exact merge')
    await waitFor(() => expect(run).toBeEnabled())
    fireEvent.click(run)
    await waitFor(() => expect(mocks.submit).toHaveBeenCalledTimes(1))
    expect(mocks.submit.mock.calls[0][0].graph.nodes.map((node: any) => node.id)).toEqual(['source', 'select', 'write'])
  })

  it('rotates the submission identity when an identity column changes', () => {
    render(<MergeColumnsControl nodeId="write" />)
    fireEvent.change(screen.getByLabelText('Merge identity columns'), { target: { value: 'id, frame' } })
    expect(mocks.state.updateConfig).toHaveBeenCalledWith('write', expect.objectContaining({ mergeColumns: expect.objectContaining({ identityColumns: ['id', 'frame'] }) }))
    const value = mocks.state.updateConfig.mock.calls[0][1].mergeColumns
    expect(value.submissionId).not.toBe('submission-1')
    expect(value.taskId).toBeUndefined()
  })

  it('rotates the id when the Source exact revision changes, but keeps it for response-loss replay', async () => {
    const view = render(<MergeColumnsControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    await screen.findByText('Eligible exact merge')
    const first = mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.mergeColumns.submissionId
    // Same semantic request remains eligible to replay after a lost submit response.
    mocks.submit.mockRejectedValueOnce(new Error('response lost'))
    fireEvent.click(screen.getByRole('button', { name: 'Run column merge' }))
    await waitFor(() => expect(mocks.submit).toHaveBeenCalled())
    expect(mocks.submit.mock.calls[0][0].submissionId).toBe(first)
    // A user-selected new exact Source revision is a new admission, never a replay of prior work.
    mocks.state.doc.nodes.find((node: any) => node.id === 'source').data.config.datasetRef.revisionId = 'rev-2'
    view.rerender(<MergeColumnsControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    await waitFor(() => expect(mocks.preflight).toHaveBeenCalledTimes(2))
    expect(mocks.preflight.mock.calls[1][0].submissionId).not.toBe(first)
  })

  it('fences an old preflight response after an edit and explains stale heads without auto-rebase', async () => {
    let resolve: ((value: typeof preflight) => void) | undefined
    mocks.preflight.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    render(<MergeColumnsControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    fireEvent.change(screen.getByLabelText('Merge identity columns'), { target: { value: 'id, frame' } })
    resolve?.(preflight)
    await waitFor(() => expect(screen.queryByText('Eligible exact merge')).not.toBeInTheDocument())

    mocks.preflight.mockRejectedValueOnce(new Error('destination head must equal the exact Source revision'))
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    expect(await screen.findByText(/Nothing has been retargeted/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Reset for a new admission' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Use current head and recompute' })).toBeVisible()
    expect(screen.queryByText('Re-admit current head')).not.toBeInTheDocument()
  })

  it('retargets Source uri and exact revision only after the explicit current-head action', async () => {
    mocks.preflight.mockRejectedValueOnce(new Error('destination head must equal the exact Source revision'))
    render(<MergeColumnsControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    await screen.findByText(/Nothing has been retargeted/)
    expect(mocks.state.doc.nodes.find((node: any) => node.id === 'source').data.config.datasetRef.revisionId).toBe('rev-1')
    fireEvent.click(screen.getByRole('button', { name: 'Use current head and recompute' }))
    await waitFor(() => expect(mocks.state.updateConfig).toHaveBeenCalledWith('source', expect.objectContaining({
      uri: 'managed://dataset-1/current.parquet', tableId: 'dataset-1',
      datasetRef: expect.objectContaining({ kind: 'exact', revisionId: 'rev-current' }),
    })))
    expect(mocks.state.updateConfig).toHaveBeenCalledWith('write', expect.objectContaining({ mergeColumns: expect.objectContaining({ taskId: undefined }) }))
  })

  it('fences a late submit response after a config edit', async () => {
    let resolve: ((value: any) => void) | undefined
    mocks.submit.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    render(<MergeColumnsControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    await screen.findByText('Eligible exact merge')
    fireEvent.click(screen.getByRole('button', { name: 'Run column merge' }))
    fireEvent.change(screen.getByLabelText('Merge identity columns'), { target: { value: 'id, frame' } })
    resolve?.({ taskId: 'late-task', status: 'done', canRetry: false, canCancel: false, mergeColumns: null })
    await waitFor(() => expect(mocks.state.updateConfig.mock.calls.some(([, patch]: any[]) => patch.mergeColumns?.taskId === 'late-task')).toBe(false))
  })

  it('settles an immediate done replay without leaving submit busy', async () => {
    mocks.submit.mockResolvedValueOnce({ taskId: 'done-task', status: 'done', canRetry: false, canCancel: false, mergeColumns: { phase: 'done', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-1', candidate: 'committed', reused: true, canRetry: false, canCancel: false } })
    render(<MergeColumnsControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    await screen.findByText('Eligible exact merge')
    fireEvent.click(screen.getByRole('button', { name: 'Run column merge' }))
    await screen.findByText('done')
    expect(screen.queryByText('Submitting…')).not.toBeInTheDocument()
  })

  it('persists an ambiguous submission before POST and recovers the same id after reopening', async () => {
    const view = render(<MergeColumnsControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    await screen.findByText('Eligible exact merge')
    mocks.submit.mockRejectedValueOnce(new Error('response lost'))
    fireEvent.click(screen.getByRole('button', { name: 'Run column merge' }))
    await screen.findByRole('alert')
    const beforeClose = mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.mergeColumns
    expect(beforeClose.submissionState).toBe('response_unknown')
    expect(beforeClose.submissionId).toBeTruthy()
    view.unmount()

    mocks.submit.mockResolvedValueOnce({ taskId: 'recovered-task', status: 'queued', canRetry: false, canCancel: true,
      mergeColumns: { phase: 'validating', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-1', candidate: 'pending', reused: true, canRetry: false, canCancel: true } })
    render(<MergeColumnsControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Recover previous submission' }))
    await waitFor(() => expect(mocks.submit).toHaveBeenCalledTimes(2))
    expect(mocks.submit.mock.calls[1][0].submissionId).toBe(beforeClose.submissionId)
    expect(mocks.preflight).toHaveBeenCalledTimes(1)
    expect(mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.mergeColumns).toEqual(expect.objectContaining({
      taskId: 'recovered-task', submissionState: undefined,
    }))
  })

  it('treats a terminal stale task as a deliberate new admission, never an auto-rebase', async () => {
    const merge = mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.mergeColumns
    merge.taskId = 'stale-task'
    mocks.task.mockResolvedValueOnce({ taskId: 'stale-task', status: 'failed', canRetry: false, canCancel: false,
      mergeColumns: { phase: 'failed', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-1', candidate: 'rejected', reused: false,
        diagnosticCode: 'stale_expected_head', canRetry: false, canCancel: false } })
    render(<MergeColumnsControl nodeId="write" />)
    await screen.findByText(/The destination moved/)
    fireEvent.click(screen.getByRole('button', { name: 'Start new admission' }))
    expect(mocks.state.updateConfig).toHaveBeenLastCalledWith('write', expect.objectContaining({ mergeColumns: expect.objectContaining({
      taskId: undefined, submissionState: undefined,
    }) }))
    expect(mocks.tableByRegistration).not.toHaveBeenCalled()
  })

  it('clears a failed cancel action rather than leaving task controls fenced', async () => {
    const merge = mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.mergeColumns
    merge.taskId = 'active-task'
    mocks.task.mockResolvedValueOnce({ taskId: 'active-task', status: 'running', canRetry: false, canCancel: true,
      mergeColumns: { phase: 'writing', baseDatasetId: 'dataset-1', baseRevisionId: 'rev-1', candidate: 'candidate', reused: false, canRetry: false, canCancel: true } })
    mocks.cancel.mockRejectedValueOnce(new Error('cancel transport failed'))
    render(<MergeColumnsControl nodeId="write" />)
    const cancel = await screen.findByRole('button', { name: 'Cancel' })
    fireEvent.click(cancel)
    await screen.findByText('cancel transport failed')
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })
})
