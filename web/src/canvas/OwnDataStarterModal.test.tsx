import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { StrictMode } from 'react'
import type { CatalogTable } from '../types/api'
import type { ColumnSchema } from '../types/graph'

const mocks = vi.hoisted(() => ({ tablesPage: vi.fn(), tableByRegistration: vi.fn(), schema: vi.fn(), create: vi.fn() }))
vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../store/graph', () => ({ useStore: { getState: () => ({ newFromStarter: mocks.create }) } }))

import { OwnDataStarterModal } from './OwnDataStarterModal'

const column = (name: string, type: string): ColumnSchema => ({ name, type, capabilities: [] })
const table: CatalogTable = {
  id: 'catalog-orders', registrationId: 'registration-orders', name: 'Orders', uri: '/data/orders.parquet',
  columns: [column('stale', 'int64')], rowCount: 120,
}
const other: CatalogTable = { ...table, id: 'catalog-other', registrationId: 'registration-other', name: 'Other orders', uri: '/data/other.parquet' }
const columns = [column('sale "USD"', 'decimal128(20, 4)'), column('quantity', 'int64'), column('label', 'string'), column('items', 'list<int64>')]
const page = (items = [table], hasMore = false) => ({ items, hasMore, total: items.length })
const choose = async (name = 'Orders') => {
  fireEvent.click(await screen.findByRole('button', { name: `Choose starter dataset ${name}` }))
}
const fill = async (value = '123456789012345.6789') => {
  await choose()
  await waitFor(() => expect(screen.getByLabelText('Starter numeric column')).toBeEnabled())
  fireEvent.change(screen.getByLabelText('Starter numeric column'), { target: { value: 'sale "USD"' } })
  fireEvent.change(screen.getByLabelText('Starter threshold'), { target: { value } })
}

describe('OwnDataStarterModal', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.tablesPage.mockResolvedValue(page())
    mocks.tableByRegistration.mockImplementation(async (id) => id === other.registrationId ? other : table)
    mocks.schema.mockResolvedValue({ src: { out: columns } })
    mocks.create.mockResolvedValue({ ok: true, canvasId: 'created', persistence: 'remote' })
  })

  it('uses exact registration identity and fresh Source columns, preserving the typed threshold', async () => {
    const onOpenChange = vi.fn()
    render(<OwnDataStarterModal open onOpenChange={onOpenChange} intent="replace-pristine" />)
    await fill()

    expect(mocks.tableByRegistration).toHaveBeenCalledWith('registration-orders')
    expect(mocks.schema).toHaveBeenCalledWith(expect.objectContaining({ nodes: [expect.objectContaining({
      id: 'src', type: 'source', data: expect.objectContaining({ config: {
        uri: table.uri, tableId: table.id, registrationId: table.registrationId,
      } }),
    })], edges: [] }), 'src')
    expect(screen.queryByRole('option', { name: /stale/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /label|items/ })).not.toBeInTheDocument()
    expect(mocks.create).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Use in this Canvas' }))
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith({
      kind: 'numeric-filter', table: { ...table, columns }, column: 'sale "USD"', threshold: '123456789012345.6789',
    }, 'replace-pristine'))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('builds a group count from fresh text columns without requiring a numeric threshold', async () => {
    const onCreate = vi.fn().mockResolvedValue({ ok: true, canvasId: 'counts', persistence: 'remote' })
    render(<OwnDataStarterModal open kind="group-count" onOpenChange={() => {}} onCreate={onCreate} />)
    await choose()
    await waitFor(() => expect(screen.getByLabelText('Starter grouping column')).toBeEnabled())
    expect(screen.queryByRole('option', { name: /stale/ })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'label (string)' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Starter threshold')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create count Canvas' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Starter grouping column'), { target: { value: 'label' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create count Canvas' }))
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith({ kind: 'group-count', table: { ...table, columns }, column: 'label' }))
    expect(mocks.create).not.toHaveBeenCalled()
  })

  it('keeps schema selection working under StrictMode and uses physical numeric types', async () => {
    mocks.schema.mockResolvedValue({ src: { out: [
      { ...column('decimal_value', 'float'), physicalType: 'decimal128(20, 4)' },
      { ...column('nested', 'int64'), physicalType: 'list<int64>' },
    ] } })
    render(<StrictMode><OwnDataStarterModal open onOpenChange={() => {}} /></StrictMode>)
    await choose()
    expect(await screen.findByRole('option', { name: 'decimal_value (float)' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /nested/ })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Starter numeric column')).toBeEnabled()
  })

  it('clears the previous column immediately and ignores a late schema after switching datasets', async () => {
    mocks.tablesPage.mockResolvedValue(page([table, other]))
    let finishOld!: (result: unknown) => void
    render(<OwnDataStarterModal open onOpenChange={() => {}} />)
    await fill('10')
    mocks.schema.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
    await choose()
    expect(screen.getByLabelText('Starter numeric column')).toHaveValue('')
    expect(screen.queryByRole('option', { name: /sale/ })).not.toBeInTheDocument()
    await waitFor(() => expect(mocks.schema).toHaveBeenCalledTimes(2))
    mocks.schema.mockResolvedValueOnce({ src: { out: [column('new_number', 'double')] } })
    await choose('Other orders')
    expect(await screen.findByRole('option', { name: 'new_number (double)' })).toBeInTheDocument()
    await act(async () => finishOld({ src: { out: columns } }))
    expect(screen.queryByRole('option', { name: /sale/ })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Starter numeric column')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Create filter Canvas' })).toBeDisabled()
  })

  it.each([
    [[], 'This dataset has no columns.'],
    [[column('label', 'string'), column('items', 'list<int64>')], 'This dataset has no numeric columns.'],
  ])('explains an unusable schema and blocks creation', async (schema, expected) => {
    mocks.schema.mockResolvedValue({ src: { out: schema } })
    render(<OwnDataStarterModal open onOpenChange={() => {}} />)
    await choose()
    expect(await screen.findByText(expected, { exact: false })).toBeVisible()
    expect(screen.getByLabelText('Starter numeric column')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Create filter Canvas' })).toBeDisabled()
  })

  it.each([null, undefined])('rejects an unknown live schema without falling back to catalog columns', async (schema) => {
    mocks.schema.mockResolvedValue({ src: { out: schema } })
    render(<OwnDataStarterModal open onOpenChange={() => {}} />)
    await choose()
    expect(await screen.findByRole('alert')).toHaveTextContent('columns could not be read')
    expect(screen.queryByRole('option', { name: /stale/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create filter Canvas' })).toBeDisabled()
    mocks.schema.mockResolvedValue({ src: { out: columns } })
    fireEvent.click(screen.getByRole('button', { name: 'Retry dataset columns' }))
    expect(await screen.findByRole('option', { name: /sale/ })).toBeInTheDocument()
  })

  it('keeps schema failures actionable and refuses a changed registration identity', async () => {
    mocks.schema.mockRejectedValueOnce(new Error('Data source offline'))
    render(<OwnDataStarterModal open onOpenChange={() => {}} />)
    await choose()
    expect(await screen.findByRole('alert')).toHaveTextContent('Data source offline')
    mocks.tableByRegistration.mockResolvedValue({ ...table, registrationId: 'replacement-registration' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry dataset columns' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('selected dataset has changed')
    expect(mocks.schema).toHaveBeenCalledTimes(1)
    expect(mocks.create).not.toHaveBeenCalled()
  })

  it.each(['', '0x10', 'Infinity', 'NaN', '1 OR TRUE', '1e999'])('blocks invalid threshold %j', async (value) => {
    render(<OwnDataStarterModal open onOpenChange={() => {}} />)
    await fill(value)
    expect(screen.getByRole('button', { name: 'Create filter Canvas' })).toBeDisabled()
    expect(mocks.create).not.toHaveBeenCalled()
  })

  it('preserves a failed draft for retry and blocks repeated submission while creating', async () => {
    let finish!: (result: unknown) => void
    mocks.create.mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    const onOpenChange = vi.fn()
    render(<OwnDataStarterModal open onOpenChange={onOpenChange} />)
    await fill('-1.2e3')
    const button = screen.getByRole('button', { name: 'Create filter Canvas' })
    fireEvent.click(button)
    fireEvent.click(button)
    expect(mocks.create).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await act(async () => finish({ ok: false }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Your selections are kept')
    expect(screen.getByLabelText('Starter numeric column')).toHaveValue('sale "USD"')
    expect(screen.getByLabelText('Starter threshold')).toHaveValue('-1.2e3')
    expect(onOpenChange).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Create filter Canvas' }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
  })

  it('uses an explicit folder creation callback when supplied', async () => {
    const onCreate = vi.fn().mockResolvedValue({ ok: true, canvasId: 'in-folder', persistence: 'remote' })
    render(<OwnDataStarterModal open onOpenChange={() => {}} onCreate={onCreate} />)
    await fill('0')
    fireEvent.click(screen.getByRole('button', { name: 'Create filter Canvas' }))
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({ kind: 'numeric-filter', threshold: '0' })))
    expect(mocks.create).not.toHaveBeenCalled()
  })

  it('retries search, paginates, and discards stale search responses', async () => {
    mocks.tablesPage.mockRejectedValueOnce(new Error('Catalog unavailable')).mockResolvedValue(page([table], true))
    render(<OwnDataStarterModal open onOpenChange={() => {}} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Catalog unavailable')
    fireEvent.click(screen.getByRole('button', { name: 'Retry dataset search' }))
    await screen.findByRole('button', { name: 'Choose starter dataset Orders' })
    mocks.tablesPage.mockResolvedValueOnce(page([other]))
    fireEvent.click(screen.getByRole('button', { name: 'Load more datasets' }))
    expect(await screen.findByRole('button', { name: 'Choose starter dataset Other orders' })).toBeVisible()
    expect(mocks.tablesPage).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 12 }))
    let finishOld!: (result: unknown) => void
    mocks.tablesPage.mockImplementation(({ q }) => q === 'old'
      ? new Promise((resolve) => { finishOld = resolve }) : Promise.resolve(page([other])))
    fireEvent.change(screen.getByLabelText('Search starter datasets'), { target: { value: 'old' } })
    await waitFor(() => expect(mocks.tablesPage).toHaveBeenCalledWith(expect.objectContaining({ q: 'old' })))
    fireEvent.change(screen.getByLabelText('Search starter datasets'), { target: { value: 'new' } })
    await screen.findByRole('button', { name: 'Choose starter dataset Other orders' })
    await act(async () => finishOld(page([table])))
    expect(screen.queryByRole('button', { name: 'Choose starter dataset Orders' })).not.toBeInTheDocument()
  })

  it('disables missing and unregistered datasets and clears the form after closing', async () => {
    mocks.tablesPage.mockResolvedValue(page([{ ...table, missing: true }, { ...other, registrationId: null }]))
    const { rerender } = render(<OwnDataStarterModal open onOpenChange={() => {}} />)
    expect(await screen.findByRole('button', { name: 'Choose starter dataset Orders' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Choose starter dataset Other orders' })).toBeDisabled()
    mocks.tablesPage.mockResolvedValue(page())
    rerender(<OwnDataStarterModal open={false} onOpenChange={() => {}} />)
    rerender(<OwnDataStarterModal open onOpenChange={() => {}} />)
    await fill('20')
    rerender(<OwnDataStarterModal open={false} onOpenChange={() => {}} />)
    rerender(<OwnDataStarterModal open onOpenChange={() => {}} />)
    expect(screen.getByLabelText('Starter numeric column')).toHaveValue('')
    expect(screen.getByLabelText('Starter threshold')).toHaveValue('')
  })
})
