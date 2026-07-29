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

describe('WritePublicationSummary task-first output states', () => {
  it('shows the server-admitted dataset name instead of the editable filename interpretation', () => {
    const admission = {
      nodeId: 'write', managed: true, provider: 'managed-local-file', mode: 'create',
      destination: '/outputs/family_cost.parquet', expectedSchema: [], partitions: [],
      intent: { destination: { name: 'family_cost' } },
    } as any

    render(<WritePublicationSummary outputName="family_cost.parquet"
      destination="Workspace outputs" admission={admission} />)

    expect(screen.getByText('Dataset name')).toBeVisible()
    expect(screen.getByText('family_cost')).toBeVisible()
    expect(screen.getByText('Workspace outputs')).toBeVisible()
    expect(screen.queryByText('family_cost.parquet')).not.toBeInTheDocument()
  })

  it('keeps an unchecked output quiet and then shows writing progress in direct language', () => {
    const admission = {
      nodeId: 'write', managed: true, provider: 'managed-local-file', mode: 'create',
      destination: '/outputs/family_cost.parquet', expectedSchema: [], partitions: [],
      intent: { destination: { name: 'family_cost' } },
    } as any
    const { rerender } = render(<WritePublicationSummary outputName="family cost"
      destination="Workspace outputs" />)

    expect(screen.getByText('Output name')).toBeVisible()
    expect(screen.getByText('family cost')).toBeVisible()
    expect(screen.queryByText('Mode')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Write readiness')).toHaveTextContent('Checking output…')

    rerender(<WritePublicationSummary outputName="family cost" destination="Workspace outputs"
      admission={admission} publishing />)
    expect(screen.getByText('Dataset name')).toBeVisible()
    expect(screen.getByText('family_cost')).toBeVisible()
    expect(screen.getByLabelText('Write readiness')).toHaveTextContent('Writing output…')
  })

  it('explains runtime-schema output checking without asking for an inferred contract', () => {
    const admission = {
      nodeId: 'write', managed: true, provider: 'managed-local-file', mode: 'create',
      destination: '/outputs/runtime.parquet', expectedSchema: [], partitions: [],
      intent: { schemaMode: 'runtime', destination: { name: 'runtime' } },
    } as any

    render(<WritePublicationSummary outputName="runtime.parquet"
      destination="Workspace outputs" admission={admission} />)

    const summary = screen.getByLabelText('Write publication')
    expect(screen.getByLabelText('Write readiness')).toHaveTextContent(
      'Ready to run. Output columns will be checked during the run.')
    expect(summary).not.toHaveTextContent('bounded output schema contract')
    expect(summary).not.toHaveTextContent('Infer from sample')
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

    expect(screen.getAllByText('published')).toHaveLength(2)
    expect(screen.queryByText('next')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Published result')).toHaveTextContent('Output published')
    expect(screen.getByLabelText('Published result')).toHaveTextContent('published · version revision-7 · 2 rows')
  })

  it('reloads exact schema comparison evidence from the receipt alone', () => {
    const withDrift = {
      ...receipt,
      parentHead: { kind: 'exact', datasetId: 'dataset-1', revisionId: 'revision-6' },
      schemaDrift: {
        comparedHead: { kind: 'exact', datasetId: 'dataset-1', revisionId: 'revision-6' },
        compatibility: { status: 'unknown', fields: [{
          kind: 'changed', status: 'unknown', fieldId: 'field-1',
          oldName: 'amount', newName: 'amount',
          reason: 'field identity is missing or changed, so the name match is not proven stable',
        }] },
        requiresConfirmation: true,
      },
    } as any

    render(<WritePublicationSummary outputName="output" destination="Workspace outputs"
      receipt={withDrift} completed />)

    const details = screen.getByText('Technical details').closest('details')!
    expect(details).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText('Technical details'))
    const comparisons = screen.getAllByLabelText('Schema comparison')
    expect(comparisons[0]).toHaveTextContent('dataset-1@revision-6')
    expect(comparisons[0]).toHaveTextContent('changed · unknown')
    expect(comparisons[0]).toHaveTextContent('Structural schema drift requires explicit confirmation')
  })

  it('does not promise a dataset for provider-neutral output', () => {
    const admission = {
      nodeId: 'write', managed: false, provider: 'plugin-sink', mode: 'overwrite',
      destination: 's3://example/output.parquet', expectedSchema: [], partitions: [],
    } as any
    render(<WritePublicationSummary outputName="output.parquet" destination="External destination"
      outcomeAdmission={admission} completed />)

    const summary = screen.getByLabelText('Write publication')
    expect(summary).toHaveTextContent('Output name')
    expect(summary).toHaveTextContent('Overwrite provider output')
    expect(summary).toHaveTextContent('Run finished. The selected backend wrote the output.')
    expect(screen.queryByRole('button', { name: 'View published version' })).not.toBeInTheDocument()
  })

  it('keeps the completed admission in the task-first summary after active admission cleanup or replacement', () => {
    const outcomeAdmission = {
      nodeId: 'write', managed: true, provider: 'managed-local-file', mode: 'create', destination: 'managed://dataset-1',
      expectedSchema: [], partitions: [],
    } as any
    const nextAdmission = { ...outcomeAdmission, mode: 'replace', blocker: 'Next run is blocked' }
    const { rerender } = render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs"
      outcomeAdmission={outcomeAdmission} receipt={receipt} completed />)

    const summaryMode = screen.getByText('Mode').parentElement!
    expect(within(summaryMode).getByText('Create a new dataset')).toBeVisible()

    rerender(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs"
      admission={nextAdmission} outcomeAdmission={outcomeAdmission} receipt={receipt} completed />)
    expect(within(screen.getByText('Mode').parentElement!).getByText('Create a new dataset')).toBeVisible()
    expect(screen.queryByLabelText('Write blocker')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Write readiness')).not.toBeInTheDocument()

    rerender(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    expect(screen.queryByText('Mode')).not.toBeInTheDocument()
    expect(screen.queryByText('Revision mode is not available yet')).not.toBeInTheDocument()
  })

  it('opens only the receipt-backed exact revision and fails closed when it is unavailable', async () => {
    mocks.datasetRevision.mockRejectedValueOnce(new Error('revision compacted'))
    render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    fireEvent.click(screen.getByRole('button', { name: 'View published version' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('A newer version was not substituted')
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
    fireEvent.click(screen.getByRole('button', { name: 'View published version' }))
    await waitFor(() => expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('Published dataset · version revision-7'))
    expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('2 rows · 1 schema field')
    expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('Name published')
    expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('Parent revision-6')
  })

  it('clears a previously opened detail before a later exact lookup fails', async () => {
    mocks.datasetRevision.mockResolvedValueOnce({ datasetId: 'dataset-1', revisionId: 'revision-7', summary: {}, preview: { columns: [] } })
      .mockRejectedValueOnce(new Error('permission lost'))
    render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    const action = screen.getByRole('button', { name: 'View published version' })
    fireEvent.click(action)
    await screen.findByLabelText('Exact revision detail')
    fireEvent.click(action)
    expect(await screen.findByRole('alert')).toHaveTextContent('A newer version was not substituted')
    expect(screen.queryByLabelText('Exact revision detail')).not.toBeInTheDocument()
  })

  it('cannot install stale exact detail after the receipt changes', async () => {
    let resolveFirst!: (value: unknown) => void
    mocks.datasetRevision.mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce({
        datasetId: 'dataset-1', revisionId: 'revision-8', summary: { rowCount: 3 }, preview: { columns: [] },
      })
    const { rerender } = render(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={receipt} completed />)
    fireEvent.click(screen.getByRole('button', { name: 'View published version' }))

    const nextReceipt = { ...receipt, revisionId: 'revision-8', head: { ...receipt.head, revisionId: 'revision-8' } }
    rerender(<WritePublicationSummary outputName="output.parquet" destination="Workspace outputs" receipt={nextReceipt} completed />)
    fireEvent.click(screen.getByRole('button', { name: 'View published version' }))
    await waitFor(() => expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('Published dataset · version revision-8'))

    await act(async () => resolveFirst({
      datasetId: 'dataset-1', revisionId: 'revision-7', summary: { rowCount: 2 }, preview: { columns: [] },
    }))
    expect(screen.getByLabelText('Exact revision detail')).toHaveTextContent('Published dataset · version revision-8')
    expect(screen.getByLabelText('Exact revision detail')).not.toHaveTextContent('revision-7')
  })
})
