import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ lineage: vi.fn() }))
vi.mock('../api/client', () => ({ api: mocks }))

import { DatasetLineageSummary } from './DatasetLineageSummary'

describe('DatasetLineageSummary', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.lineage.mockResolvedValue({
      rootUri: 'workspace-provider://source',
      nodes: [
        { id: 'source', name: 'raw_video', uri: 'workspace-provider://source', kind: 'dataset' },
        { id: 'output', name: 'frames', uri: 'file:///frames.parquet', kind: 'dataset' },
      ],
      edges: [{ parent: 'workspace-provider://source', child: 'file:///frames.parquet', factCount: 1 }],
      truncated: false,
    })
  })

  it('embeds recorded inputs and outputs and opens a linked dataset', async () => {
    const onOpenDataset = vi.fn()
    render(<DatasetLineageSummary uri="workspace-provider://source" name="raw_video" onOpenDataset={onOpenDataset} />)

    const summary = await screen.findByTestId('dataset-lineage-summary')
    expect(within(summary).getByText('raw_video')).toBeVisible()
    expect(within(summary).getByRole('button', { name: 'frames' })).toBeVisible()
    fireEvent.click(within(summary).getByRole('button', { name: 'frames' }))
    expect(onOpenDataset).toHaveBeenCalledWith('output')
    expect(mocks.lineage).toHaveBeenCalledWith('workspace-provider://source', 4, 60)
  })

  it('keeps a failed lineage read retryable', async () => {
    mocks.lineage.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({
      rootUri: 'workspace-provider://source', nodes: [], edges: [], truncated: false,
    })
    render(<DatasetLineageSummary uri="workspace-provider://source" name="raw_video" />)

    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't load lineage: offline")
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText('No recorded inputs or outputs yet.')).toBeVisible())
  })

  it('shows unregistered lineage endpoints without offering a broken dataset link', async () => {
    mocks.lineage.mockResolvedValueOnce({
      rootUri: 'workspace-provider://source',
      nodes: [
        { id: 'source', name: 'raw_video', uri: 'workspace-provider://source', kind: 'dataset' },
        { id: 'file:///raw.parquet', name: 'raw', uri: 'file:///raw.parquet', kind: 'dataset' },
        { id: 'file:///frames.parquet', name: 'frames', uri: 'file:///frames.parquet', kind: 'dataset' },
      ],
      edges: [
        { parent: 'file:///raw.parquet', child: 'workspace-provider://source', factCount: 1 },
        { parent: 'workspace-provider://source', child: 'file:///frames.parquet', factCount: 1 },
      ],
      truncated: false,
    })
    const onOpenDataset = vi.fn()
    render(<DatasetLineageSummary uri="workspace-provider://source" name="raw_video" onOpenDataset={onOpenDataset} />)

    const summary = await screen.findByTestId('dataset-lineage-summary')
    expect(within(summary).getByText('raw')).toBeVisible()
    expect(within(summary).getByText('frames')).toBeVisible()
    expect(within(summary).queryByRole('button', { name: 'raw' })).toBeNull()
    expect(within(summary).queryByRole('button', { name: 'frames' })).toBeNull()
    expect(onOpenDataset).not.toHaveBeenCalled()
  })
})
