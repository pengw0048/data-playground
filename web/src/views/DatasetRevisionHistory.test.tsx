import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CatalogTable, DatasetRevisionDetail } from '../types/api'

const mocks = vi.hoisted(() => ({ datasetRevisions: vi.fn(), datasetRevision: vi.fn() }))
vi.mock('../api/client', () => ({
  api: mocks,
  KernelError: class KernelError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

import { KernelError } from '../api/client'
import { DatasetRevisionHistory } from './DatasetRevisionHistory'

const TABLE: CatalogTable = { id: 'table-1', name: 'orders', uri: 'lance:///orders', columns: [] }
const revision = (revisionId: string) => ({
  datasetId: 'dataset-stable', revisionId, committedAt: '2026-07-16T12:00:00Z', retentionOwner: 'provider' as const,
})
const detail = (revisionId: string, overrides: Partial<DatasetRevisionDetail> = {}): DatasetRevisionDetail => ({
  ...revision(revisionId), parentRevisionId: null, producerOperation: null,
  summary: { rowCount: 2, dataFileCount: 1, totalBytes: 20, fragmentCount: 1 },
  preview: {
    columns: [{ fieldId: 'amount', name: 'amount', type: 'bigint', nullable: false, provenance: 'provider', capabilities: [] }],
    rows: [{ amount: 2 }], hasMore: false, rowLimit: 100,
  },
  ...overrides,
})

describe('DatasetRevisionHistory', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => cleanup())

  it('hides the entry point when the provider lacks the capability', async () => {
    mocks.datasetRevisions.mockRejectedValue(new KernelError(501, 'history unavailable'))
    render(<DatasetRevisionHistory table={TABLE} />)
    await waitFor(() => expect(screen.queryByTestId('dataset-revision-history')).toBeNull())
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
})
