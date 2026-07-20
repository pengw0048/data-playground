import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  state: {} as any,
  preflight: vi.fn(), submit: vi.fn(), task: vi.fn(), revision: vi.fn(), cancel: vi.fn(), retry: vi.fn(),
}))

vi.mock('../store/graph', () => ({
  roleCanEdit: () => true,
  useStore: (selector: (state: any) => unknown) => selector(mocks.state),
}))
vi.mock('../api/client', () => ({
  api: {
    upsertPreflight: mocks.preflight, submitUpsert: mocks.submit, upsertTask: mocks.task,
    datasetRevision: mocks.revision, cancelUpsertTask: mocks.cancel, retryUpsertTask: mocks.retry,
  },
  KernelError: class KernelError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

import { UpsertControl } from './UpsertControl'

const preflight = {
  base: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'rev-1' },
  head: { kind: 'exact' as const, datasetId: 'payload-1', revisionId: 'prev-1' },
  expectedHead: { kind: 'exact' as const, datasetId: 'dataset-1', revisionId: 'rev-1' },
  keys: ['id'], outputSchema: [{ name: 'id', type: 'int' }, { name: 'value', type: 'string' }],
  evidence: { matched: 2, inserted: 1, unchanged: 1, rejected: 0, duplicate: 0, conflict: 0 },
  eligible: true,
}

function baseState(configOverride: any = { keys: ['id'] }) {
  return {
    canvasRole: 'owner',
    runs: { write: { writeAdmission: { provider: 'managed-local-file', mode: 'replace', expectedHead: { kind: 'exact', datasetId: 'dataset-1', revisionId: 'rev-1' } } } },
    doc: { id: 'canvas-1', version: 1, requirements: [], parameters: [], nodes: [
      { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', config: { uri: 'payload.parquet', datasetRef: { kind: 'exact', datasetId: 'payload-1', revisionId: 'prev-1' } } } },
      { id: 'write', type: 'write', position: { x: 1, y: 0 }, data: { title: 'write', config: { filename: 'target.parquet', keyedUpsert: { submissionId: 'submission-1', ...configOverride } } } },
    ], edges: [{ id: 'a', source: 'source', target: 'write' }] },
    updateConfig: vi.fn((id: string, patch: any) => {
      const node = mocks.state.doc.nodes.find((item: any) => item.id === id)
      node.data.config = { ...node.data.config, ...patch }
    }),
  }
}

describe('UpsertControl', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.state = baseState()
    mocks.preflight.mockResolvedValue(preflight)
    mocks.submit.mockResolvedValue({ taskId: 'task-1', status: 'queued', datasetId: 'dataset-1', expectedHeadRevisionId: 'rev-1', payloadDatasetId: 'payload-1', payloadRevisionId: 'prev-1', canCancel: true, canRetry: false })
  })

  it('projects preflight evidence then requires it before submitting', async () => {
    render(<UpsertControl nodeId="write" />)
    const run = screen.getByRole('button', { name: 'Run keyed upsert' })
    expect(run).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    await screen.findByText('Eligible keyed upsert')
    expect(screen.getByLabelText('Upsert projection')).toHaveTextContent('2 matched · 1 inserted · 1 unchanged')
    await waitFor(() => expect(run).toBeEnabled())
    fireEvent.click(run)
    await waitFor(() => expect(mocks.submit).toHaveBeenCalledTimes(1))
    expect(mocks.submit.mock.calls[0][0]).toMatchObject({ datasetId: 'dataset-1', expectedHeadRevisionId: 'rev-1', payloadDatasetId: 'payload-1', payloadRevisionId: 'prev-1', keys: ['id'] })
  })

  it('rotates the submission identity when the key columns change', () => {
    render(<UpsertControl nodeId="write" />)
    fireEvent.change(screen.getByLabelText('Upsert key columns'), { target: { value: 'id, frame' } })
    expect(mocks.state.updateConfig).toHaveBeenCalledWith('write', expect.objectContaining({ keyedUpsert: expect.objectContaining({ keys: ['id', 'frame'] }) }))
    expect(mocks.state.updateConfig.mock.calls[0][1].keyedUpsert.submissionId).not.toBe('submission-1')
  })

  it('renders a fail-closed typed error from preflight without submitting', async () => {
    mocks.preflight.mockRejectedValue(new Error('upsert rejected null or duplicate keys (rejected=0, duplicate=1, conflict=0)'))
    render(<UpsertControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    await screen.findByText(/duplicate=1/)
    expect(mocks.submit).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Run keyed upsert' })).toBeDisabled()
  })

  it('surfaces a moved head as permanent and offers only a new admission', async () => {
    mocks.state = baseState({ keys: ['id'], taskId: 'task-1' })
    mocks.task.mockResolvedValue({ taskId: 'task-1', status: 'failed', datasetId: 'dataset-1', expectedHeadRevisionId: 'rev-1', payloadDatasetId: 'payload-1', payloadRevisionId: 'prev-1', diagnosticCode: 'stale_expected_head', canCancel: false, canRetry: false })
    render(<UpsertControl nodeId="write" />)
    await screen.findByText(/The destination moved/)
    expect(screen.getByRole('button', { name: 'Start new admission' })).toBeEnabled()
  })

  it('shows the published evidence and exact revision after a done run', async () => {
    mocks.state = baseState({ keys: ['id'], taskId: 'task-1' })
    mocks.task.mockResolvedValue({ taskId: 'task-1', status: 'done', datasetId: 'dataset-1', expectedHeadRevisionId: 'rev-1', payloadDatasetId: 'payload-1', payloadRevisionId: 'prev-1', childRevisionId: 'rev-2', canCancel: false, canRetry: false, evidence: { matched: 2, inserted: 1, unchanged: 1, rejected: 0, duplicate: 0, conflict: 0 }, receipt: { datasetId: 'dataset-1', revisionId: 'rev-2', rows: 4, bytes: 128, schema: [], partitions: [], publication: {} as any, provenance: {} as any, durable: true, head: { datasetId: 'dataset-1', revisionId: 'rev-2', retentionOwner: 'core' }, parentHead: { kind: 'exact', datasetId: 'dataset-1', revisionId: 'rev-1' } } })
    render(<UpsertControl nodeId="write" />)
    await screen.findByText('2 matched · 1 inserted · 1 unchanged')
    expect(screen.getByText('Published exact revision')).toBeInTheDocument()
    expect(screen.getByText('dataset-1@rev-2')).toBeInTheDocument()
  })

  it('never advertises the mode for an unsupported destination', () => {
    const state = baseState({})
    state.runs.write.writeAdmission = { provider: 'managed-local-file', mode: 'create', expectedHead: null }
    state.doc.nodes.find((n: any) => n.id === 'write').data.config.keyedUpsert = {}
    mocks.state = state
    const { container } = render(<UpsertControl nodeId="write" />)
    expect(container).toBeEmptyDOMElement()
  })
})
