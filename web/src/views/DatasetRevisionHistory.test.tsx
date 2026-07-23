import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CatalogTable, DatasetRevisionDetail, DatasetViewDefinition } from '../types/api'

const mocks = vi.hoisted(() => ({
  datasetRevisions: vi.fn(), datasetRevision: vi.fn(), datasetRevisionCapabilities: vi.fn(),
  createDatasetView: vi.fn(), restoreRevision: vi.fn(), restoreRevisionTask: vi.fn(),
}))
const store = vi.hoisted(() => ({
  pushToast: vi.fn(), setWorkspaceResource: vi.fn(), switchWorkspaceScope: vi.fn(),
  workspaceScope: 'datasets' as 'all' | 'datasets', workspaceResourceId: 'dataset:table-1' as string | null,
}))
vi.mock('../api/client', () => ({
  api: mocks,
  KernelError: class KernelError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))
vi.mock('../store/graph', () => ({ useStore: (select: (state: typeof store) => unknown) => select(store) }))

import { KernelError } from '../api/client'
import { parseHash, routeHash } from '../router'
import { DatasetRevisionHistory } from './DatasetRevisionHistory'

const TABLE: CatalogTable = { id: 'table-1', registrationId: 'registration-current', name: 'orders', uri: 'lance:///orders', columns: [] }
const revision = (revisionId: string) => ({
  datasetId: 'dataset-stable', revisionId, committedAt: '2026-07-16T12:00:00Z', retentionOwner: 'provider' as const,
})
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((next) => { resolve = next })
  return { promise, resolve }
}
const detail = (revisionId: string, overrides: Partial<DatasetRevisionDetail> = {}): DatasetRevisionDetail => ({
  ...revision(revisionId), parentRevisionId: null, producerOperation: null,
  summary: { rowCount: 2, dataFileCount: 1, totalBytes: 20, fragmentCount: 1 },
  preview: {
    columns: [{ fieldId: 'amount', name: 'amount', type: 'bigint', nullable: false, provenance: 'provider', capabilities: [] }],
    rows: [{ amount: 2 }], hasMore: false, rowLimit: 100,
  },
  ...overrides,
})
const VIEW: DatasetViewDefinition = {
  schemaVersion: 1,
  id: 'view-1',
  creatorId: 'local',
  name: 'orders view',
  datasetRef: { kind: 'exact', datasetId: 'dataset-stable', revisionId: 'rev-2', lastKnown: { committedAt: '2026-07-16T12:00:00Z' } },
  placement: { containerId: 'workspace-local-root', placementId: 'placement-view-1', sourceRegistrationId: 'table-1' },
  selectedColumns: ['amount'],
  predicate: null,
  sampling: { kind: 'reservoir', size: 1000, seed: 2_147_483_647 },
  sampleProvenance: {
    strategy: 'reservoir', seed: 2_147_483_647, requestedRows: 1000, scannedRows: 2, returnedRows: 2,
    totalRows: 2, datasetIdentity: 'dataset-stable', datasetRevision: 'rev-2', identity: 'sample-identity', limitations: [],
  },
  retentionOwner: 'provider',
  createdAt: '2026-07-18T12:00:00Z',
  semanticSha256: 'a'.repeat(64),
  definitionSha256: 'b'.repeat(64),
}

describe('DatasetRevisionHistory', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: ['exact', 'latest'], asOfOrdering: null, timezone: null, datasetViewSave: true,
    })
    store.workspaceScope = 'datasets'
    store.workspaceResourceId = 'dataset:table-1'
    store.switchWorkspaceScope.mockImplementation((scope: 'all' | 'datasets') => {
      store.workspaceScope = scope
      store.workspaceResourceId = null
    })
    store.setWorkspaceResource.mockImplementation((resourceId: string | null) => {
      store.workspaceResourceId = resourceId
    })
  })
  afterEach(() => { cleanup(); window.location.hash = '' })

  it('hides the entry point when the provider lacks the capability', async () => {
    mocks.datasetRevisions.mockRejectedValue(new KernelError(501, 'history unavailable'))
    render(<DatasetRevisionHistory table={TABLE} />)
    await waitFor(() => expect(screen.queryByTestId('dataset-revision-history')).toBeNull())
  })

  it('opens the requested historical revision directly and never substitutes the current head', async () => {
    mocks.datasetRevisions.mockResolvedValue({ items: [revision('rev-current')], nextCursor: null, hasMore: false })
    mocks.datasetRevision.mockResolvedValue(detail('rev-historical'))
    render(<DatasetRevisionHistory table={TABLE} initialRevisionId="rev-historical" initialRevisionDatasetId="logical-receipt-id" />)
    await waitFor(() => expect(mocks.datasetRevision).toHaveBeenCalledWith('logical-receipt-id', 'rev-historical'))
    expect(await screen.findByText('Exact revision rev-historical')).toBeInTheDocument()
    expect(mocks.datasetRevision).not.toHaveBeenCalledWith('logical-receipt-id', 'rev-current')
  })

  it('does not treat a revision without its logical dataset identity as an exact deep link', async () => {
    mocks.datasetRevisions.mockResolvedValue({ items: [revision('rev-current')], nextCursor: null, hasMore: false })
    render(<DatasetRevisionHistory table={TABLE} initialRevisionId="rev-historical" />)
    await screen.findByText('rev-current')
    expect(mocks.datasetRevision).not.toHaveBeenCalled()
  })

  it('distinguishes empty, unavailable, and provider-error history states', async () => {
    mocks.datasetRevisions.mockResolvedValueOnce({ items: [], nextCursor: null, hasMore: false })
    const first = render(<DatasetRevisionHistory table={TABLE} />)
    expect(await screen.findByText('No retained revisions are available.')).toBeInTheDocument()
    first.unmount()

    mocks.datasetRevisions.mockRejectedValueOnce(new KernelError(410, 'gone'))
    const second = render(<DatasetRevisionHistory table={TABLE} />)
    expect(await screen.findByText(/Revision history is unavailable.*No latest revision was substituted/i)).toBeInTheDocument()
    second.unmount()

    mocks.datasetRevisions.mockRejectedValueOnce(new KernelError(503, 'provider offline'))
      .mockResolvedValueOnce({ items: [], nextCursor: null, hasMore: false })
    render(<DatasetRevisionHistory table={TABLE} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('No retained revisions are available.')).toBeInTheDocument()
  })

  it('uses the opaque cursor and keeps already loaded revisions on a load-more failure', async () => {
    mocks.datasetRevisions
      .mockResolvedValueOnce({ items: [revision('rev-2')], nextCursor: 'opaque cursor', hasMore: true })
      .mockRejectedValueOnce(new KernelError(503, 'page failed'))
      .mockResolvedValueOnce({ items: [revision('rev-1')], nextCursor: null, hasMore: false })
    render(<DatasetRevisionHistory table={TABLE} />)
    expect(await screen.findByText('rev-2')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('revision-history-load-more'))
    expect(await screen.findByText(/Couldn't load more history: page failed/i)).toBeInTheDocument()
    expect(screen.getByText('rev-2')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('revision-history-load-more'))
    expect(await screen.findByText('rev-1')).toBeInTheDocument()
    expect(mocks.datasetRevisions).toHaveBeenLastCalledWith(TABLE.id, { limit: 20, cursor: 'opaque cursor' })
  })

  it('keeps a slow save capability probe current while revision pagination advances', async () => {
    const capability = deferred<{
      selectors: ('exact' | 'latest')[]
      asOfOrdering: null
      timezone: null
      datasetViewSave: boolean
    }>()
    mocks.datasetRevisionCapabilities.mockReturnValue(capability.promise)
    mocks.datasetRevisions
      .mockResolvedValueOnce({ items: [revision('rev-2')], nextCursor: 'next-page', hasMore: true })
      .mockResolvedValueOnce({ items: [revision('rev-1')], nextCursor: null, hasMore: false })
    mocks.datasetRevision.mockResolvedValue(detail('rev-2'))
    render(<DatasetRevisionHistory table={TABLE} />)

    fireEvent.click(await screen.findByTestId('revision-history-load-more'))
    expect(await screen.findByText('rev-1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open revision rev-2' }))
    expect(await screen.findByText('Exact revision rev-2')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save view' })).toBeNull()

    capability.resolve({
      selectors: ['exact', 'latest'], asOfOrdering: null, timezone: null, datasetViewSave: true,
    })
    expect(await screen.findByRole('button', { name: 'Save view' })).toBeInTheDocument()
  })

  it('opens the selected identity exactly and compares its retained parent honestly', async () => {
    mocks.datasetRevisions.mockResolvedValue({ items: [revision('rev-2')], nextCursor: null, hasMore: false })
    mocks.datasetRevision.mockImplementation((_datasetId: string, revisionId: string) => revisionId === 'rev-2'
      ? Promise.resolve(detail('rev-2', {
        parentRevisionId: 'rev-1', producerOperation: 'append',
        summary: { rowCount: 4, dataFileCount: 2, totalBytes: 45, fragmentCount: 2 },
        preview: {
          columns: [{ fieldId: 'amount', name: 'amount', type: 'int', nullable: false, provenance: 'provider', capabilities: [] }],
          rows: [{ amount: 4 }], hasMore: true, rowLimit: 100,
        },
      }))
      : Promise.resolve(detail('rev-1')))
    render(<DatasetRevisionHistory table={TABLE} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open revision rev-2' }))

    expect(await screen.findByText('Exact revision rev-2')).toBeInTheDocument()
    expect(screen.getByText(/Parent rev-1 · producer append/)).toBeInTheDocument()
    expect(screen.getByText('breaking')).toBeInTheDocument()
    expect(screen.getByText(/logical type narrows from bigint to int/i)).toBeInTheDocument()
    expect(screen.getByText(/Preview truncated at 100 rows.*exact revision/i)).toBeInTheDocument()
    expect(mocks.datasetRevision).toHaveBeenNthCalledWith(1, 'dataset-stable', 'rev-2')
    expect(mocks.datasetRevision).toHaveBeenNthCalledWith(2, 'dataset-stable', 'rev-1')
  })

  it('never falls back to latest when the selected exact revision was compacted', async () => {
    mocks.datasetRevisions.mockResolvedValue({ items: [revision('rev-old')], nextCursor: null, hasMore: false })
    mocks.datasetRevision.mockRejectedValue(new KernelError(410, 'compacted'))
    render(<DatasetRevisionHistory table={TABLE} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open revision rev-old' }))
    expect(await screen.findByText(/no longer retained.*did not substitute latest/i)).toBeInTheDocument()
    expect(mocks.datasetRevision).toHaveBeenCalledTimes(1)
  })

  it('hides Save view when the server does not advertise local exact DatasetView support', async () => {
    mocks.datasetRevisionCapabilities.mockResolvedValue({
      selectors: ['exact', 'latest'], asOfOrdering: null, timezone: null, datasetViewSave: false,
    })
    mocks.datasetRevisions.mockResolvedValue({ items: [revision('rev-2')], nextCursor: null, hasMore: false })
    mocks.datasetRevision.mockResolvedValue(detail('rev-2'))
    render(<DatasetRevisionHistory table={TABLE} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Open revision rev-2' }))
    expect(await screen.findByText('Exact revision rev-2')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save view' })).toBeNull()
  })

  it('keeps an in-flight exact save visible and reuses its submission identity on retry', async () => {
    mocks.datasetRevisions.mockResolvedValue({ items: [revision('rev-2')], nextCursor: null, hasMore: false })
    mocks.datasetRevision.mockResolvedValue(detail('rev-2'))
    let rejectFirst!: (reason: Error) => void
    const firstAttempt = new Promise<DatasetViewDefinition>((_resolve, reject) => { rejectFirst = reject })
    mocks.createDatasetView.mockReturnValueOnce(firstAttempt).mockResolvedValueOnce(VIEW)
    render(<DatasetRevisionHistory table={TABLE} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Open revision rev-2' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save view' }))
    const dialog = await screen.findByRole('dialog', { name: 'Save exact revision as view' })
    fireEvent.click(within(dialog).getByRole('radio', { name: /Deterministic reservoir/ }))
    fireEvent.change(within(dialog).getByLabelText('Reservoir seed'), { target: { value: '2147483647' } })
    expect(dialog).toHaveTextContent('Each preview replays that full scan; the rows are not materialized.')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save view' }))

    await waitFor(() => expect(mocks.createDatasetView).toHaveBeenCalledTimes(1))
    const firstRequest = mocks.createDatasetView.mock.calls[0][0]
    expect(within(dialog).getByRole('button', { name: 'Close save view dialog' })).toBeDisabled()
    fireEvent.click(dialog.parentElement!)
    expect(screen.getByRole('dialog', { name: 'Save exact revision as view' })).toBeVisible()

    rejectFirst(new Error('connection reset'))
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't save this view: connection reset")
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save view' }))
    await waitFor(() => expect(mocks.createDatasetView).toHaveBeenCalledTimes(2))
    expect(mocks.createDatasetView.mock.calls[1][0].submissionId).toBe(firstRequest.submissionId)
    expect(firstRequest).toMatchObject({
      name: 'orders view',
      datasetRef: { kind: 'exact', datasetId: 'dataset-stable', revisionId: 'rev-2' },
      selectedColumns: ['amount'],
      sampling: { kind: 'reservoir', size: 1000, seed: 2_147_483_647 },
    })
    await waitFor(() => expect(store.setWorkspaceResource).toHaveBeenCalledWith('dataset_view:view-1'))
    expect(store.switchWorkspaceScope).toHaveBeenCalledWith('all')
    expect(store.switchWorkspaceScope.mock.invocationCallOrder[0])
      .toBeLessThan(store.setWorkspaceResource.mock.invocationCallOrder[0])
    expect(store.workspaceScope).toBe('all')
    expect(store.workspaceResourceId).toBe('dataset_view:view-1')
    window.location.hash = routeHash(
      'workspace', undefined, store.workspaceResourceId ?? undefined, undefined, undefined,
      undefined, undefined, store.workspaceScope,
    )
    expect(parseHash()).toEqual({ view: 'workspace', workspaceResourceId: 'dataset_view:view-1' })
    expect(window.location.hash).not.toContain('scope=datasets')
    expect(store.pushToast).toHaveBeenCalledWith('Saved “orders view” beside its source in Workspace', 'success')
  })

  const coreDetail = (revisionId: string) => detail(revisionId, { retentionOwner: 'core' })
  const openForRestore = async (opened: string) => {
    mocks.datasetRevisions.mockResolvedValue({
      items: [revision('rev-head'), revision('rev-old')], nextCursor: null, hasMore: false,
    })
    mocks.datasetRevision.mockImplementation((_dataset: string, revisionId: string) =>
      Promise.resolve(coreDetail(revisionId)))
    render(<DatasetRevisionHistory table={TABLE} />)
    fireEvent.click(await screen.findByRole('button', { name: `Open revision ${opened}` }))
    await screen.findByText(`Exact revision ${opened}`)
  }

  it('publishes an old core revision as a new head and reopens the exact result', async () => {
    await openForRestore('rev-old')
    mocks.restoreRevision.mockResolvedValue({
      taskId: 'task-1', status: 'done', sourceDatasetId: 'dataset-stable', sourceRevisionId: 'rev-old',
      expectedHeadRevisionId: 'rev-head', childRevisionId: 'rev-new', receipt: null,
    })
    fireEvent.click(screen.getByTestId('restore-revision'))
    const dialog = await screen.findByRole('dialog', { name: 'Restore revision as new head' })
    fireEvent.click(within(dialog).getByTestId('restore-revision-confirm'))

    await waitFor(() => expect(mocks.restoreRevision).toHaveBeenCalledTimes(1))
    const [datasetId, revisionId, body] = mocks.restoreRevision.mock.calls[0]
    expect([datasetId, revisionId]).toEqual(['dataset-stable', 'rev-old'])
    expect(body.expectedHeadRevisionId).toBe('rev-head')
    expect(typeof body.submissionId).toBe('string')
    await waitFor(() => expect(store.pushToast).toHaveBeenCalledWith(
      'Published revision rev-new from the restored source', 'success'))
    await waitFor(() => expect(mocks.datasetRevision).toHaveBeenCalledWith('dataset-stable', 'rev-new'))
  })

  it('surfaces a moving-head conflict without reporting success', async () => {
    await openForRestore('rev-old')
    mocks.restoreRevision.mockResolvedValue({
      taskId: 'task-1', status: 'failed', sourceDatasetId: 'dataset-stable', sourceRevisionId: 'rev-old',
      expectedHeadRevisionId: 'rev-head', childRevisionId: null, diagnosticCode: 'stale_expected_head',
    })
    fireEvent.click(screen.getByTestId('restore-revision'))
    const dialog = await screen.findByRole('dialog', { name: 'Restore revision as new head' })
    fireEvent.click(within(dialog).getByTestId('restore-revision-confirm'))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/current head changed/i)
    expect(store.pushToast).not.toHaveBeenCalled()
  })

  it('offers restore only for retained core revisions that are not the current head', async () => {
    mocks.datasetRevisions.mockResolvedValue({
      items: [revision('rev-head'), revision('rev-old')], nextCursor: null, hasMore: false,
    })
    mocks.datasetRevision.mockImplementation((_dataset: string, revisionId: string) => Promise.resolve(
      revisionId === 'rev-head' ? coreDetail('rev-head')
        : detail('rev-old', { retentionOwner: 'provider' })))
    render(<DatasetRevisionHistory table={TABLE} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open revision rev-head' }))
    await screen.findByText('Exact revision rev-head')
    expect(screen.queryByTestId('restore-revision')).toBeNull()  // the current head has nothing to restore
    fireEvent.click(screen.getByRole('button', { name: 'Open revision rev-old' }))
    await screen.findByText('Exact revision rev-old')
    expect(screen.queryByTestId('restore-revision')).toBeNull()  // provider-owned history is not core-restorable
  })

  it('rejects a reservoir seed above the DuckDB signed 32-bit contract', async () => {
    mocks.datasetRevisions.mockResolvedValue({ items: [revision('rev-2')], nextCursor: null, hasMore: false })
    mocks.datasetRevision.mockResolvedValue(detail('rev-2'))
    render(<DatasetRevisionHistory table={TABLE} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Open revision rev-2' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save view' }))
    const dialog = await screen.findByRole('dialog', { name: 'Save exact revision as view' })
    fireEvent.click(within(dialog).getByRole('radio', { name: /Deterministic reservoir/ }))
    fireEvent.change(within(dialog).getByLabelText('Reservoir seed'), { target: { value: '2147483648' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save view' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'seed must be between 0 and 2,147,483,647',
    )
    expect(mocks.createDatasetView).not.toHaveBeenCalled()
  })
})
