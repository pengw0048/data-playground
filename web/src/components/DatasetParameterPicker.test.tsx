import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { datasetRevisionTimeLabel } from '../lib/revisionTime'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  tablesPage: vi.fn(), tableByRegistration: vi.fn(), datasetRevisionCapabilities: vi.fn(),
  resolveDatasetRevision: vi.fn(), datasetRevisions: vi.fn(), datasetRevision: vi.fn(), change: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))

import { DatasetParameterPicker, type DatasetParameterValue } from './DatasetParameterPicker'

const table = { id: 'catalog-entry', registrationId: 'registration-entry', name: 'Orders', uri: 'managed://orders', columns: [], description: 'Daily purchases', rowCount: 10 }
const head = { datasetId: 'logical-orders', revisionId: 'rev-new', committedAt: '2026-09-25T10:30:00Z', retentionOwner: 'core', selector: 'latest' }
const old = { ...head, revisionId: 'rev-old', committedAt: '2026-09-24T10:30:00Z' }
function Harness({ initial }: { initial?: DatasetParameterValue }) {
  const [value, setValue] = useState<DatasetParameterValue | undefined>(initial)
  return <DatasetParameterPicker label="Input" value={value} onChange={(next) => { mocks.change(next); setValue(next) }} />
}

describe('DatasetParameterPicker', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.tablesPage.mockResolvedValue({ items: [table], hasMore: false, total: 1 })
    mocks.tableByRegistration.mockResolvedValue(table)
    mocks.datasetRevisionCapabilities.mockResolvedValue({ selectors: ['exact', 'latest'], datasetViewSave: true })
    mocks.resolveDatasetRevision.mockResolvedValue(head)
    mocks.datasetRevisions.mockResolvedValue({ items: [head, old], hasMore: false })
    mocks.datasetRevision.mockImplementation(async (datasetId, revisionId) => ({
      ...old, datasetId, revisionId, name: 'Orders', producerOperation: 'replace', summary: { rowCount: 10 },
    }))
  })

  it('searches names and binds only authoritative dataset identity, then pins a readable saved version', async () => {
    render(<Harness />)
    fireEvent.change(screen.getByLabelText('Input dataset search'), { target: { value: 'Orders' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Choose dataset Orders' }))
    await waitFor(() => expect(mocks.change).toHaveBeenCalledWith({ kind: 'latest', datasetId: 'logical-orders' }))
    expect(mocks.tablesPage).toHaveBeenCalledWith(expect.objectContaining({ q: 'Orders' }))
    expect(mocks.resolveDatasetRevision).toHaveBeenCalledWith('catalog-entry')
    await waitFor(() => expect(screen.getByLabelText('Input selection')).toBeEnabled())
    expect(screen.getAllByText('Daily purchases', { selector: 'span' })[0]).toBeVisible()
    fireEvent.change(screen.getByLabelText('Input selection'), { target: { value: 'exact' } })
    expect(screen.getByRole('option', { name: `${datasetRevisionTimeLabel(old.committedAt, 'core')} · rev-old` })).toHaveValue('rev-old')
    fireEvent.change(screen.getByLabelText('Input version'), { target: { value: 'rev-old' } })
    expect(mocks.change).toHaveBeenLastCalledWith({ kind: 'exact', datasetId: 'logical-orders', revisionId: 'rev-old' })
    expect(await screen.findByText(/replace · 10 rows/)).toBeVisible()
    expect(screen.getByLabelText('Input version')).toHaveValue('rev-old')
  })

  it.each([410, 403, 503])('preserves an unavailable saved exact version for status %s without falling back to latest', async (status) => {
    mocks.datasetRevision.mockRejectedValue({ status })
    render(<Harness initial={{ kind: 'exact', datasetId: 'logical-orders', revisionId: 'rev-deleted' }} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Your selection is unchanged; it will not switch to latest automatically.')
    expect(screen.getByLabelText('Input version')).toHaveValue('rev-deleted')
    expect(mocks.change).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByLabelText('Input selection')).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Input selection'), { target: { value: 'latest' } })
    expect(mocks.change).toHaveBeenCalledWith({ kind: 'latest', datasetId: 'logical-orders' })
  })

  it('hydrates a disabled declared default by canonical identity without creating an override', async () => {
    render(<DatasetParameterPicker label="Input" value={{ kind: 'latest', datasetId: 'logical-orders' }} disabled onChange={mocks.change} />)
    expect(await screen.findByText('Orders')).toBeVisible()
    expect(mocks.tableByRegistration).toHaveBeenCalledWith('logical-orders')
    expect(screen.getByLabelText('Input selection')).toBeDisabled()
    expect(screen.getByLabelText('Input dataset search')).toBeDisabled()
    expect(mocks.tablesPage).not.toHaveBeenCalled()
    expect(mocks.change).not.toHaveBeenCalled()
  })

  it('rejects a replaced catalog identity and leaves the saved binding intact', async () => {
    mocks.resolveDatasetRevision.mockResolvedValue({ ...head, datasetId: 'replacement-dataset' })
    render(<Harness initial={{ kind: 'latest', datasetId: 'logical-orders' }} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('now points to another dataset')
    expect(mocks.change).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Input selection')).toBeDisabled()
    expect(screen.getByLabelText('Input dataset search')).toBeEnabled()
  })

  it.each(['viewer', 'renamed', 'binding'])('does not overwrite %s state after an in-flight dataset choice', async (change) => {
    let resolve!: (value: typeof head) => void
    mocks.resolveDatasetRevision.mockReturnValue(new Promise((done) => { resolve = done }))
    const { rerender } = render(<DatasetParameterPicker label="Input" value={undefined} onChange={mocks.change} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Choose dataset Orders' }))
    await waitFor(() => expect(mocks.resolveDatasetRevision).toHaveBeenCalled())
    rerender(<DatasetParameterPicker label={change === 'renamed' ? 'Other input' : 'Input'}
      value={change === 'binding' ? { kind: 'exact', datasetId: 'logical-orders', revisionId: 'pinned' } : undefined}
      disabled={change === 'viewer'} onChange={mocks.change} />)
    await act(async () => resolve(head))
    expect(mocks.change).not.toHaveBeenCalled()
    if (change === 'viewer') expect(screen.getByLabelText('Input dataset search')).toBeDisabled()
  })

  it('ignores late search pages and does not let unsupported datasets replace a valid binding', async () => {
    let resolve!: (value: unknown) => void
    mocks.tablesPage.mockImplementation(({ q }) => q === 'old' ? new Promise((done) => { resolve = done })
      : Promise.resolve({ items: q === 'new' ? [{ ...table, name: 'New orders' }] : [table], hasMore: false }))
    render(<Harness />)
    fireEvent.change(screen.getByLabelText('Input dataset search'), { target: { value: 'old' } })
    await waitFor(() => expect(mocks.tablesPage).toHaveBeenCalledWith(expect.objectContaining({ q: 'old' })))
    fireEvent.change(screen.getByLabelText('Input dataset search'), { target: { value: 'new' } })
    const choice = await screen.findByRole('button', { name: 'Choose dataset New orders' })
    await act(async () => resolve({ items: [{ ...table, name: 'Old orders' }], hasMore: false }))
    expect(screen.queryByRole('button', { name: 'Choose dataset Old orders' })).not.toBeInTheDocument()
    mocks.datasetRevisionCapabilities.mockResolvedValue({ selectors: [], datasetViewSave: false })
    fireEvent.click(choice)
    expect(await screen.findByRole('alert')).toHaveTextContent('does not support saved versions')
    expect(mocks.change).not.toHaveBeenCalled()
  })

  it('loads further version pages without changing the current pin', async () => {
    mocks.datasetRevisions.mockResolvedValueOnce({ items: [head], hasMore: true, nextCursor: 'page-2' })
      .mockResolvedValueOnce({ items: [old], hasMore: false })
    render(<Harness initial={{ kind: 'exact', datasetId: 'logical-orders', revisionId: 'rev-new' }} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load more versions' }))
    expect(await screen.findByRole('option', { name: /rev-old/ })).toBeVisible()
    expect(mocks.datasetRevisions).toHaveBeenLastCalledWith('catalog-entry', { limit: 12, cursor: 'page-2' })
    expect(within(screen.getByLabelText('Input version')).getAllByRole('option')).toHaveLength(2)
    expect(mocks.change).not.toHaveBeenCalled()
  })

  it('retries a failed new choice explicitly instead of reloading the old binding', async () => {
    mocks.resolveDatasetRevision.mockRejectedValueOnce(new Error('Source offline')).mockResolvedValue(head)
    render(<Harness />)
    fireEvent.click(await screen.findByRole('button', { name: 'Choose dataset Orders' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Select the dataset again to retry.')
    expect(screen.queryByRole('button', { name: 'Retry dataset' })).not.toBeInTheDocument()
    expect(mocks.change).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Input dataset search'), { target: { value: 'Orders' } })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Choose dataset Orders' }))
    await waitFor(() => expect(mocks.change).toHaveBeenCalledWith({ kind: 'latest', datasetId: 'logical-orders' }))
  })
})
