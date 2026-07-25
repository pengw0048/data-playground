import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ datasetRevision: vi.fn() }))
vi.mock('../api/client', () => ({ api: mocks }))

import { WritePublicationSummary } from './WritePublicationSummary'

const receipt = {
  datasetId: 'dataset-1', revisionId: 'revision-7', name: 'output', rows: 2, bytes: 128, durable: true,
  head: { datasetId: 'dataset-1', revisionId: 'revision-7', committedAt: '2026-07-21T12:00:00Z', retentionOwner: 'core' },
  schema: [{ name: 'id', type: 'bigint' }], partitions: [], publication: {
    provider: 'managed-local-file', logicalUri: 'managed://dataset-1', artifactUri: 'file:///revision-7.parquet',
    publishSequence: 7, idempotencyKey: 'write-7', catalogVersion: 'catalog-7', backendVersion: '8.0.0',
  }, executionManifestSha256: 'a'.repeat(64),
} as any

describe('WritePublicationSummary exact receipt action', () => {
  it('shows the server-admitted logical name instead of the editable filename interpretation', () => {
    const admission = {
      nodeId: 'write', managed: true, provider: 'managed-local-file', mode: 'create',
      destination: '/outputs/family_cost.parquet', expectedSchema: [], partitions: [],
      intent: { destination: { name: 'family_cost' } },
    } as any

    render(<WritePublicationSummary outputName="family_cost.parquet"
      destination="Workspace outputs" admission={admission} />)

    expect(screen.getByText('family_cost')).toBeVisible()
    expect(screen.queryByText('family_cost.parquet')).not.toBeInTheDocument()
  })

  it('keeps the receipt name authoritative when a later admission targets another name', () => {
    const nextAdmission = {
      nodeId: 'write', managed: true, provider: 'managed-local-file', mode: 'create',
      destination: '/outputs/next.parquet', expectedSchema: [], partitions: [],
      intent: { destination: { name: 'next' } },
    } as any

    render(<WritePublicationSummary outputName="editable.parquet"
      destination="Workspace outputs" admission={nextAdmission}
      receipt={{ ...receipt, name: 'published' }} completed />)

    expect(screen.getByText('published')).toBeVisible()
    expect(screen.queryByText('next')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Published result')).toHaveTextContent('published published')
  })

  it('keeps the completed admission in the task-first summary after active admission cleanup or replacement', () => {
    const outcomeAdmission = {
      nodeId: 'write', managed: true, provider: 'managed-local-file', mode: 'create', destination: 'managed://dataset-1',
      expectedSchema: [], partitions: [],
    } as any
    const nextAdmission = { ...outcomeAdmission, mode: 'replace', blocker: 'Next run is blocked' }
    const { rerender } = render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs"
      outcomeAdmission={outcomeAdmission} receipt={receipt} completed />)

    const summaryMode = screen.getByText('Publication mode').parentElement!
    expect(within(summaryMode).getByText('Create a new dataset')).toBeVisible()

    rerender(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs"
      admission={nextAdmission} outcomeAdmission={outcomeAdmission} receipt={receipt} completed />)
    expect(within(screen.getByText('Publication mode').parentElement!).getByText('Create a new dataset')).toBeVisible()
    expect(screen.queryByLabelText('Write blocker')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Write readiness')).toHaveTextContent('Exact publication receipt recorded')

    rerender(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    expect(within(screen.getByText('Publication mode').parentElement!)
      .getByText('Publication mode is not available yet')).toBeVisible()
  })

  it('opens only the receipt-backed exact revision and fails closed when it is unavailable', async () => {
    mocks.datasetRevision.mockRejectedValueOnce(new Error('revision compacted'))
    render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    fireEvent.click(screen.getByRole('button', { name: 'Open exact revision' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Latest was not substituted')
    expect(mocks.datasetRevision).toHaveBeenCalledTimes(1)
    expect(mocks.datasetRevision).toHaveBeenCalledWith('dataset-1', 'revision-7')
  })

  it('shows an inline exact result only after the exact receipt lookup succeeds', async () => {
    mocks.datasetRevision.mockResolvedValueOnce({
      datasetId: 'dataset-1', revisionId: 'revision-7', committedAt: '2026-07-21T12:00:00Z',
      name: 'published', parentRevisionId: 'revision-6', summary: { rowCount: 2 },
      preview: { columns: [{ name: 'id' }] },
    })
    render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    fireEvent.click(screen.getByRole('button', { name: 'Open exact revision' }))
    await waitFor(() => expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('dataset-1@revision-7'))
    expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('2 rows · 1 schema field')
    expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('Name published')
    expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('Parent revision-6')
  })

  it('clears a previously opened detail before a later exact lookup fails', async () => {
    mocks.datasetRevision.mockResolvedValueOnce({ datasetId: 'dataset-1', revisionId: 'revision-7', summary: {}, preview: { columns: [] } })
      .mockRejectedValueOnce(new Error('permission lost'))
    render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    const action = screen.getByRole('button', { name: 'Open exact revision' })
    fireEvent.click(action)
    await screen.findByLabelText('Exact revision detail')
    fireEvent.click(action)
    expect(await screen.findByRole('alert')).toHaveTextContent('Latest was not substituted')
    expect(screen.queryByLabelText('Exact revision detail')).not.toBeInTheDocument()
  })

  it('cannot install stale exact detail after the receipt changes', async () => {
    let resolveFirst!: (value: unknown) => void
    mocks.datasetRevision.mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce({
        datasetId: 'dataset-1', revisionId: 'revision-8', summary: { rowCount: 3 }, preview: { columns: [] },
      })
    const { rerender } = render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    fireEvent.click(screen.getByRole('button', { name: 'Open exact revision' }))

    const nextReceipt = { ...receipt, revisionId: 'revision-8', head: { ...receipt.head, revisionId: 'revision-8' } }
    rerender(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={nextReceipt} completed />)
    fireEvent.click(screen.getByRole('button', { name: 'Open exact revision' }))
    await waitFor(() => expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('dataset-1@revision-8'))

    await act(async () => resolveFirst({
      datasetId: 'dataset-1', revisionId: 'revision-7', summary: { rowCount: 2 }, preview: { columns: [] },
    }))
    expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('dataset-1@revision-8')
    expect(screen.getByLabelText('Exact revision detail')).not.toHaveTextContent('revision-7')
  })
})
