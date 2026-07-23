import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  state: {} as any,
  tablesPage: vi.fn(), resolve: vi.fn(), table: vi.fn(), revision: vi.fn(), preflight: vi.fn(), submit: vi.fn(), task: vi.fn(),
  cancel: vi.fn(), retry: vi.fn(),
}))

vi.mock('../store/graph', () => ({
  roleCanEdit: (role: string) => role !== 'viewer',
  useStore: (selector: (state: any) => unknown) => selector(mocks.state),
}))
vi.mock('../router', () => ({ routeHash: (...parts: unknown[]) => `#/${parts.filter(Boolean).join('/')}` }))
vi.mock('../api/client', () => ({
  api: {
    tablesPage: mocks.tablesPage, resolveDatasetRevision: mocks.resolve, tableByRegistration: mocks.table, datasetRevision: mocks.revision,
    managedSidecarMergePreflight: mocks.preflight, submitManagedSidecarMerge: mocks.submit,
    managedSidecarMergeTask: mocks.task, cancelManagedSidecarMergeTask: mocks.cancel,
    retryManagedSidecarMergeTask: mocks.retry,
  },
  KernelError: class KernelError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } },
}))

import { ManagedSidecarMergeControl } from './ManagedSidecarMergeControl'

const base = { id: 'base-dataset', registrationId: 'base-dataset', name: 'wide base', uri: 'managed://base', version: 'parquet', columns: [{ name: 'id', type: 'int32' }, { name: 'keep', type: 'string' }], keys: [{ columns: ['id'], confidence: 'declared' as const }] }
const sidecar = { id: 'sidecar-dataset', registrationId: 'sidecar-dataset', name: 'sidecar', uri: 'managed://sidecar', version: 'parquet', columns: [{ name: 'id', type: 'int32' }, { name: 'score', type: 'float64' }] }
const configured = (extra: Record<string, unknown> = {}) => ({ managedSidecarMerge: {
  base: { kind: 'exact', datasetId: 'base-dataset', revisionId: 'base-r4' }, identityColumns: ['id'],
  rules: [{ source: 'score', target: 'score', mode: 'add' }], ...extra,
} })
const eligiblePreflight = () => ({
  base: { kind: 'exact', datasetId: 'base-dataset', revisionId: 'base-r4' },
  sidecar: { kind: 'exact', datasetId: 'sidecar-dataset', revisionId: 'sidecar-r1' },
  expectedHead: { kind: 'exact', datasetId: 'base-dataset', revisionId: 'base-r4' },
  identityColumns: ['id'], rules: [{ source: 'score', target: 'score', mode: 'add' }],
  baseSchema: base.columns, sidecarSchema: sidecar.columns,
  outputSchema: [...base.columns, { name: 'score', type: 'float64' }],
  coverage: { base: { rows: 1, uniqueIdentities: 1, nullRows: 0, duplicateGroups: 0, duplicateRows: 0 }, candidate: { rows: 1, uniqueIdentities: 1, nullRows: 0, duplicateGroups: 0, duplicateRows: 0 }, matchedIdentities: 1, missingIdentities: 0, extraIdentities: 0, status: 'complete' }, eligible: true,
})

function install({ multiple = false, role = 'owner', config = {} }: { multiple?: boolean; role?: string; config?: Record<string, unknown> } = {}) {
  mocks.state = {
    canvasRole: role, jobsQuery: '', setJobsQuery: vi.fn(),
    doc: { id: 'canvas-1', version: 1, requirements: [], parameters: [], nodes: [
      { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'sidecar', config: { uri: 'managed://sidecar', datasetRef: { kind: 'exact', datasetId: 'sidecar-dataset', revisionId: 'sidecar-r1' } } } },
      { id: 'write', type: 'write', position: { x: 1, y: 0 }, data: { title: 'write', config } },
      ...(multiple ? [{ id: 'second', type: 'source', position: { x: 0, y: 1 }, data: { title: 'second', config: { uri: 'managed://other', datasetRef: { kind: 'exact', datasetId: 'other', revisionId: 'r1' } } } }] : []),
    ], edges: multiple ? [{ id: 'a', source: 'source', target: 'write' }, { id: 'b', source: 'second', target: 'write' }] : [{ id: 'a', source: 'source', target: 'write' }] },
    updateConfig: vi.fn((id: string, patch: Record<string, unknown>) => {
      const node = mocks.state.doc.nodes.find((item: any) => item.id === id)
      node.data.config = { ...node.data.config, ...patch }
    }),
  }
}

describe('ManagedSidecarMergeControl', () => {
  beforeEach(() => {
    vi.resetAllMocks(); install()
    mocks.tablesPage.mockResolvedValue({ items: [base], total: 1, hasMore: false })
    mocks.resolve.mockResolvedValue({ datasetId: 'base-dataset', revisionId: 'base-r4', selector: 'latest' })
    mocks.table.mockImplementation((id: string) => Promise.resolve(id === 'base-dataset' ? base : sidecar))
    mocks.revision.mockImplementation((datasetId: string) => Promise.resolve({ preview: { columns: datasetId === 'base-dataset' ? base.columns : sidecar.columns } }))
  })

  it('starts with an explicit empty draft and persists only exact identities, never physical URIs', async () => {
    const view = render(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Configure managed sidecar merge' }))
    expect(mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.managedSidecarMerge).toEqual({ identityColumns: [], rules: [] })
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.change(screen.getByLabelText('Search destination bases'), { target: { value: 'wide' } })
    await screen.findByText('wide base')
    fireEvent.click(screen.getByText('wide base'))
    await waitFor(() => expect(mocks.resolve).toHaveBeenCalledWith('base-dataset'))
    const stored = mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.managedSidecarMerge
    expect(stored.base).toEqual({ kind: 'exact', datasetId: 'base-dataset', revisionId: 'base-r4' })
    expect(JSON.stringify(stored)).not.toContain('managed://')
  })

  it('offers explicit non-authoritative suggestions before preflight and fences a late response after an edit', async () => {
    const view = render(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Configure managed sidecar merge' }))
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.change(screen.getByLabelText('Search destination bases'), { target: { value: 'wide' } })
    await screen.findByText('wide base'); fireEvent.click(screen.getByText('wide base'))
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    await screen.findByRole('button', { name: 'Use id' })
    fireEvent.click(screen.getByRole('button', { name: 'Use id' }))
    expect(mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.managedSidecarMerge.identityColumns).toEqual(['id'])
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Add suggested rules' }))
    expect(mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.managedSidecarMerge.rules).toEqual([{ source: 'score', target: 'score', mode: 'add' }])

    let resolve: (value: any) => void = () => undefined
    mocks.preflight.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    fireEvent.click(screen.getByRole('button', { name: 'Check eligibility' }))
    fireEvent.change(screen.getByLabelText('Managed sidecar identity columns'), { target: { value: 'id, frame' } })
    resolve({ base: { kind: 'exact', datasetId: 'base-dataset', revisionId: 'base-r4' }, sidecar: { kind: 'exact', datasetId: 'sidecar-dataset', revisionId: 'sidecar-r1' }, expectedHead: { kind: 'exact', datasetId: 'base-dataset', revisionId: 'base-r4' }, identityColumns: ['id'], rules: [], baseSchema: [], sidecarSchema: [], outputSchema: [], coverage: { base: { rows: 1, uniqueIdentities: 1, nullRows: 0, duplicateGroups: 0, duplicateRows: 0 }, candidate: { rows: 1, uniqueIdentities: 1, nullRows: 0, duplicateGroups: 0, duplicateRows: 0 }, matchedIdentities: 1, missingIdentities: 0, extraIdentities: 0, status: 'complete' }, eligible: true })
    await waitFor(() => expect(screen.queryByText('Eligible exact sidecar merge')).not.toBeInTheDocument())
  })

  it('refuses an ambiguous multi-input graph rather than selecting an arbitrary source', () => {
    install({ multiple: true })
    const view = render(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Configure managed sidecar merge' }))
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    expect(screen.getByText(/Connect one exact managed-local Source directly/)).toBeVisible()
  })

  it('does not search the catalog until the managed-sidecar control is enabled', () => {
    render(<ManagedSidecarMergeControl nodeId="write" />)
    expect(mocks.tablesPage).not.toHaveBeenCalled()
  })

  it('retains an unknown submit outcome and retries the same durable submission identity', async () => {
    install({ config: configured() })
    mocks.preflight.mockResolvedValue(eligiblePreflight())
    mocks.submit.mockRejectedValueOnce(new Error('connection lost')).mockResolvedValueOnce({ taskId: 'task-1', status: 'queued', canCancel: true, canRetry: false })
    const view = render(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Check eligibility' }))
    await screen.findByText('Eligible exact sidecar merge')
    fireEvent.click(screen.getByRole('button', { name: 'Start managed merge' }))
    await screen.findByText('connection lost')
    const first = mocks.submit.mock.calls[0]![0]
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(screen.getByRole('button', { name: 'Recover previous submission' }))
    await waitFor(() => expect(mocks.submit).toHaveBeenCalledTimes(2))
    expect(mocks.submit.mock.calls[1]![0].submissionId).toBe(first.submissionId)
  })

  it('ignores a late submit response after another surface replaces the same-semantic submission id', async () => {
    install({ config: configured() })
    mocks.preflight.mockResolvedValue(eligiblePreflight())
    let complete!: (value: any) => void
    mocks.submit.mockImplementationOnce(() => new Promise((resolve) => { complete = resolve }))
    const view = render(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Check eligibility' }))
    await screen.findByText('Eligible exact sidecar merge')
    fireEvent.click(screen.getByRole('button', { name: 'Start managed merge' }))
    const existing = mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.managedSidecarMerge
    mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.managedSidecarMerge = { ...existing, submissionId: 'peer-submission', submissionState: undefined }
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    complete({ taskId: 'obsolete-task', status: 'queued', canCancel: true, canRetry: false })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check eligibility' })).toBeEnabled())
    const stored = mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.managedSidecarMerge
    expect(stored.taskId).toBeUndefined()
    expect(stored.submissionId).toBe('peer-submission')
  })

  it('does not let a late cancel replace a peer task with the same semantic draft', async () => {
    install({ config: configured({ taskId: 'old-task', submissionId: 'old-submission' }) })
    mocks.task.mockImplementation(async (taskId: string) => ({
      taskId, status: 'queued', canCancel: true, canRetry: false,
      mergeColumns: { phase: 'queued', candidate: 'pending', reused: false },
    }))
    let complete!: (value: any) => void
    mocks.cancel.mockImplementationOnce(() => new Promise((resolve) => { complete = resolve }))
    const view = render(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    // A peer replaces the durable task without changing the merge's semantic base/rules.
    const node = mocks.state.doc.nodes.find((item: any) => item.id === 'write')
    node.data.config.managedSidecarMerge = {
      ...node.data.config.managedSidecarMerge, taskId: 'peer-task', submissionId: 'peer-submission',
    }
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    complete({ taskId: 'old-task', status: 'cancelled', canCancel: false, canRetry: false,
      mergeColumns: { phase: 'cancelled', candidate: 'pending', reused: false } })

    await waitFor(() => expect(mocks.task).toHaveBeenCalledWith('peer-task'))
    // The finally path releases the action lock, then reloads the peer task rather than rendering
    // the obsolete terminal response. The new task's valid action remains available.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled())
    expect(screen.queryByText('cancelled')).not.toBeInTheDocument()
  })

  it('refreshes only a stale destination head, preserving the direct exact sidecar', async () => {
    install({ config: configured({ taskId: 'stale-task' }) })
    mocks.task.mockResolvedValue({ taskId: 'stale-task', status: 'failed', canCancel: false, canRetry: false, diagnosticCode: 'stale_expected_head', mergeColumns: { phase: 'failed', candidate: 'committed', reused: false } })
    mocks.resolve.mockResolvedValue({ datasetId: 'base-dataset', revisionId: 'base-r5', selector: 'latest' })
    const view = render(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh destination head' }))
    await waitFor(() => expect(mocks.resolve).toHaveBeenCalledWith('base-dataset'))
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    expect(mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.managedSidecarMerge.base).toEqual({ kind: 'exact', datasetId: 'base-dataset', revisionId: 'base-r5' })
    expect(mocks.state.doc.nodes.find((node: any) => node.id === 'source').data.config.datasetRef).toEqual({ kind: 'exact', datasetId: 'sidecar-dataset', revisionId: 'sidecar-r1' })
  })

  it('handles a stale preflight by refreshing only the destination and requiring a new check', async () => {
    install({ config: configured() })
    mocks.preflight.mockRejectedValue(new Error('destination head moved'))
    mocks.resolve.mockResolvedValue({ datasetId: 'base-dataset', revisionId: 'base-r6', selector: 'latest' })
    const view = render(<ManagedSidecarMergeControl nodeId="write" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Check eligibility' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh destination head' }))
    await waitFor(() => expect(mocks.resolve).toHaveBeenCalledWith('base-dataset'))
    view.rerender(<ManagedSidecarMergeControl nodeId="write" />)
    const stored = mocks.state.doc.nodes.find((node: any) => node.id === 'write').data.config.managedSidecarMerge
    expect(stored.base).toEqual({ kind: 'exact', datasetId: 'base-dataset', revisionId: 'base-r6' })
    expect(stored.taskId).toBeUndefined()
    expect(screen.getByRole('button', { name: 'Start managed merge' })).toBeDisabled()
  })

  it('blocks incomplete and schema-incoherent drafts before preflight without claiming server authority', async () => {
    install({ config: configured({ rules: [{ source: 'score', target: 'keep', mode: 'add' }] }) })
    const first = render(<ManagedSidecarMergeControl nodeId="write" />)
    expect(await screen.findByRole('alert')).toHaveTextContent('already exists in the destination draft')
    expect(screen.getByRole('button', { name: 'Check eligibility' })).toBeDisabled()

    first.unmount()
    install({ config: configured({ rules: [{ source: 'score', target: 'missing', mode: 'replace' }] }) })
    render(<ManagedSidecarMergeControl nodeId="write" />)
    expect(await screen.findByRole('alert')).toHaveTextContent('is not in the destination draft')
  })

  it('does not let a non-owner observe or take over another submitter’s task', async () => {
    install({ role: 'editor', config: configured({ taskId: 'owner-task' }) })
    mocks.task.mockRejectedValue(new Error('not found'))
    render(<ManagedSidecarMergeControl nodeId="write" />)
    expect(await screen.findByText(/belongs to another submitter/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Check eligibility' })).not.toBeInTheDocument()
    expect(mocks.preflight).not.toHaveBeenCalled()

    install({ role: 'viewer', config: configured() })
    const viewer = render(<ManagedSidecarMergeControl nodeId="write" />)
    expect(screen.getByRole('button', { name: 'Check eligibility' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Start managed merge' })).toBeDisabled()
    viewer.unmount()
  })
})
