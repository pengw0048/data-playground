import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DatasetViewDefinition, DatasetViewPreview, TemporalWindowV1 } from '../types/api'

const mocks = vi.hoisted(() => ({ previewDatasetView: vi.fn(), deleteDatasetView: vi.fn() }))
vi.mock('../api/client', () => ({
  api: mocks,
  KernelError: class KernelError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

import { KernelError } from '../api/client'
import { DatasetViewDetail } from './DatasetViewDetail'

const DEFINITION: DatasetViewDefinition = {
  schemaVersion: 1,
  id: 'view-1',
  creatorId: 'local',
  name: 'robot interactions',
  datasetRef: { kind: 'exact', datasetId: 'dataset-stable', revisionId: 'rev-7', lastKnown: { committedAt: '2026-07-17T12:00:00Z' } },
  placement: { containerId: 'folder-robotics', placementId: 'placement-view-1', sourceRegistrationId: 'registration-1' },
  selectedColumns: ['frame_id', 'interaction'],
  predicate: "interaction IS NOT NULL",
  sampling: { kind: 'reservoir', size: 1000, seed: 42 },
  sampleProvenance: {
    strategy: 'reservoir', seed: 42, requestedRows: 1000, scannedRows: 5000, returnedRows: 1000,
    totalRows: 5000, datasetIdentity: 'dataset-stable', datasetRevision: 'rev-7', identity: 'sample-identity',
    limitations: ['Rows are replayed from the retained exact revision.'],
  },
  retentionOwner: 'core',
  createdAt: '2026-07-18T12:00:00Z',
  semanticSha256: 'a'.repeat(64),
  definitionSha256: 'b'.repeat(64),
}

describe('DatasetViewDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.previewDatasetView.mockResolvedValue({
      columns: [
        { fieldId: 'frame_id', name: 'frame_id', type: 'bigint', nullable: false, provenance: 'provider', capabilities: [] },
        { fieldId: 'interaction', name: 'interaction', type: 'varchar', nullable: true, provenance: 'provider', capabilities: [] },
      ],
      rows: [{ frame_id: 9, interaction: 'grasp' }], rowCount: 1000, hasMore: true, rowLimit: 100,
      sampleProvenance: DEFINITION.sampleProvenance,
    })
  })
  afterEach(() => cleanup())

  it('replays the exact definition and does not allow dismissal during deletion', async () => {
    let resolveDelete!: (value: { ok: boolean; deleted: boolean }) => void
    mocks.deleteDatasetView.mockReturnValue(new Promise((resolve) => { resolveDelete = resolve }))
    const onClose = vi.fn()
    const onDeleted = vi.fn()
    render(<DatasetViewDetail definition={DEFINITION} onClose={onClose} onDeleted={onDeleted} />)

    expect(await screen.findByText('grasp')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'robot interactions' })).toHaveTextContent('revision:rev-7')
    fireEvent.click(screen.getByRole('button', { name: 'Delete view' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(mocks.deleteDatasetView).toHaveBeenCalledWith('view-1'))

    const dialog = screen.getByRole('dialog', { name: 'robot interactions' })
    expect(screen.getByRole('button', { name: 'Close DatasetView detail' })).toBeDisabled()
    fireEvent.click(dialog.parentElement!)
    expect(onClose).not.toHaveBeenCalled()
    expect(dialog).toBeVisible()

    resolveDelete({ ok: true, deleted: true })
    await waitFor(() => expect(onDeleted).toHaveBeenCalledOnce())
  })

  it('reports an unavailable exact revision without substituting the current head', async () => {
    mocks.previewDatasetView.mockRejectedValue(new KernelError(410, 'revision compacted'))
    render(<DatasetViewDetail definition={DEFINITION} onClose={vi.fn()} onDeleted={vi.fn()} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This exact revision is no longer available. The view did not substitute the current head.',
    )
    expect(mocks.previewDatasetView).toHaveBeenCalledTimes(1)
  })

  it('renders a lossless half-open temporal window without implying the whole dataset', async () => {
    const windows: TemporalWindowV1[] = [
      {
        timeField: 'frame_tick', timeDomain: 'robot-monotonic-v1',
        startTick: '-9223372036854775808', endTick: '1700000000000000001',
      },
      {
        timeField: 'frame_tick', timeDomain: 'robot-monotonic-v1',
        startTick: '1700000000000000001', endTick: '9223372036854775807',
      },
    ]
    const browserRoundTrip = JSON.parse(JSON.stringify(windows)) as TemporalWindowV1[]
    expect(browserRoundTrip).toEqual(windows)
    const definition: DatasetViewDefinition = {
      ...DEFINITION,
      predicate: null,
      temporalWindow: browserRoundTrip[1],
    }
    render(<DatasetViewDetail definition={definition} onClose={vi.fn()} onDeleted={vi.fn()} />)

    expect(await screen.findByText('grasp')).toBeInTheDocument()
    const dialog = screen.getByRole('dialog', { name: 'robot interactions' })
    expect(dialog).toHaveTextContent('No additional predicate')
    expect(dialog).not.toHaveTextContent('All rows (no predicate)')
    expect(dialog).toHaveTextContent(
      'frame_tick [1700000000000000001, 9223372036854775807)',
    )
    expect(dialog).toHaveTextContent('Time domain: robot-monotonic-v1')
  })

  it('keeps an independent exact preview alive when deletion fails', async () => {
    let resolvePreview!: (value: DatasetViewPreview) => void
    let rejectDelete!: (reason: Error) => void
    mocks.previewDatasetView.mockReturnValue(new Promise((resolve) => { resolvePreview = resolve }))
    mocks.deleteDatasetView.mockReturnValue(new Promise((_resolve, reject) => { rejectDelete = reject }))
    render(<DatasetViewDetail definition={DEFINITION} onClose={vi.fn()} onDeleted={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Delete view' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    rejectDelete(new Error('metadata unavailable'))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "Couldn't delete this view: metadata unavailable",
    )
    resolvePreview({
      columns: [{ fieldId: 'frame_id', name: 'frame_id', type: 'bigint', nullable: false, provenance: 'provider', capabilities: [] }],
      rows: [{ frame_id: 9 }], rowCount: 1, hasMore: false, rowLimit: 100, sampleProvenance: DEFINITION.sampleProvenance,
    })
    expect(await screen.findByText('9')).toBeInTheDocument()
  })
})
