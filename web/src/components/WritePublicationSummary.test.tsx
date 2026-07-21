import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ datasetRevision: vi.fn() }))
vi.mock('../api/client', () => ({ api: mocks }))

import { WritePublicationSummary } from './WritePublicationSummary'

const receipt = {
  datasetId: 'dataset-1', revisionId: 'revision-7', rows: 2, bytes: 128, durable: true,
  head: { datasetId: 'dataset-1', revisionId: 'revision-7', retentionOwner: 'core' }, schema: [], partitions: [], publication: {},
} as any

describe('WritePublicationSummary exact receipt action', () => {
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
      parentRevisionId: 'revision-6', summary: { rowCount: 2 }, preview: { columns: [{ name: 'id' }] },
    })
    render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    fireEvent.click(screen.getByRole('button', { name: 'Open exact revision' }))
    await waitFor(() => expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('dataset-1@revision-7'))
    expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('2 rows · 1 schema field')
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
})
