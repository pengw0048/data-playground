import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'

const mocks = vi.hoisted(() => ({
  destinations: vi.fn(), browseDestination: vi.fn(), mkdirDestination: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))

import { FileDialog, type OpenResult } from './FileDialog'

const DESTINATIONS = { destinations: [{ id: 'local', name: 'Workspace', backend: 'local', root: '/data' }], backends: ['local'] }
const BROWSE = { path: '', entries: [{ name: 'orders.csv', kind: 'file' as const, uri: 'file:///data/orders.csv' }], writable: true }

describe('FileDialog request and open-mutation truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.destinations.mockResolvedValue(DESTINATIONS)
    mocks.browseDestination.mockImplementation(async (_destinationId: string, path: string) => ({
      ...BROWSE, path,
    }))
    mocks.mkdirDestination.mockResolvedValue({ ok: true })
  })
  afterEach(() => cleanup())

  it('behaves as a modal, contains Tab focus, closes with Escape, and restores its trigger', async () => {
    const user = userEvent.setup()
    function Harness() {
      const [open, setOpen] = useState(false)
      return <>
        <button type="button" onClick={() => setOpen(true)}>Browse outputs</button>
        {open && <FileDialog mode="save" defaultName="results.parquet"
          onClose={() => setOpen(false)} onPick={vi.fn()} />}
      </>
    }

    render(<Harness />)
    const trigger = screen.getByRole('button', { name: 'Browse outputs' })
    await user.click(trigger)

    const dialog = screen.getByRole('dialog', { name: 'Choose output destination' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const name = screen.getByRole('textbox', { name: 'Dataset name' })
    expect(name).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Save here' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Choose output destination' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('distinguishes destination and browse failures from an empty folder and retries both', async () => {
    mocks.destinations
      .mockRejectedValueOnce(new Error('HTTP 503: destinations unavailable'))
      .mockResolvedValueOnce(DESTINATIONS)
    mocks.browseDestination
      .mockRejectedValueOnce(new Error('Failed to fetch'))
      .mockResolvedValueOnce(BROWSE)
    render(<FileDialog mode="open" onClose={vi.fn()} onPick={vi.fn()} />)

    expect(await screen.findByText(/Couldn't load places: HTTP 503/i)).toBeInTheDocument()
    expect(screen.queryByText('Empty folder.')).toBeNull()
    fireEvent.click(screen.getByTestId('file-dialog-destinations-retry'))

    expect(await screen.findByText(/Couldn't load this folder: Failed to fetch/i)).toBeInTheDocument()
    expect(screen.queryByText('Empty folder.')).toBeNull()
    fireEvent.click(screen.getByTestId('file-dialog-browse-retry'))
    expect(await screen.findByText('orders.csv')).toBeInTheDocument()
  })

  it('awaits registration, keeps the dialog/path on a 4xx, and closes only after a successful retry', async () => {
    const register = vi.fn()
      .mockRejectedValueOnce(new Error('HTTP 422: unsupported dataset'))
      .mockResolvedValueOnce(undefined)

    function Harness() {
      const [open, setOpen] = useState(true)
      const pick = async (result: OpenResult) => { await register(result); setOpen(false) }
      return open
        ? <FileDialog mode="open" title="Register dataset" onClose={() => setOpen(false)} onPick={pick} />
        : <div>dialog closed</div>
    }

    render(<Harness />)
    fireEvent.click(await screen.findByText('orders.csv'))
    expect(await screen.findByText(/Couldn't open file: HTTP 422/i)).toBeInTheDocument()
    expect(screen.getByText('orders.csv')).toBeInTheDocument()
    expect(screen.getAllByText('Workspace')).toHaveLength(2)

    fireEvent.click(screen.getByText('orders.csv'))
    await waitFor(() => expect(screen.getByText('dialog closed')).toBeInTheDocument())
    expect(register).toHaveBeenCalledTimes(2)
  })

  it('browses a configured save location and returns the selected relative folder', async () => {
    mocks.destinations.mockResolvedValueOnce({
      destinations: [
        { id: 'managed', name: 'Workspace outputs', backend: 'local', root: '/outputs' },
        { id: 'external', name: 'External provider', backend: 'plugin', root: 'provider://exports' },
      ],
      backends: ['local', 'plugin'],
    })
    mocks.browseDestination.mockImplementation(async (destinationId: string, path: string) => ({
      path,
      entries: path
        ? [{ name: 'existing.parquet', kind: 'file' as const, uri: `provider://exports/${path}/existing.parquet` }]
        : [
            { name: 'daily', kind: 'dir' as const, uri: `provider://exports/daily` },
            { name: 'orders.parquet', kind: 'file' as const, uri: `provider://exports/orders.parquet` },
          ],
      writable: true,
      destinationId,
    }))
    const pick = vi.fn()
    render(<FileDialog mode="save" defaultName="results" onClose={vi.fn()} onPick={pick} />)

    expect(await screen.findByText('Destinations')).toBeVisible()
    expect(screen.getByText('Choose output destination')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'External provider' }))
    expect(await screen.findByText('orders.parquet')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'daily' }))
    expect(await screen.findByText('existing.parquet')).toBeVisible()
    expect(screen.queryByText(/managed revision|versioned revision/i)).not.toBeInTheDocument()
    expect(screen.getByText('Dataset name')).toBeVisible()
    expect(screen.getByRole('button', { name: 'New folder' })).toBeVisible()
    expect(mocks.browseDestination).toHaveBeenCalledWith('external', 'daily')
    expect(mocks.mkdirDestination).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Save here' }))

    expect(pick).toHaveBeenCalledWith({
      destId: 'external', destName: 'External provider', path: 'daily', filename: 'results',
    })
  })

  it('keeps the exact dataset name and blocks invalid names until corrected', async () => {
    const pick = vi.fn()
    render(
      <FileDialog mode="save" defaultName=" padded.parquet " onClose={vi.fn()} onPick={pick} />,
    )

    await screen.findByText('orders.csv')
    const input = screen.getByRole('textbox', { name: 'Dataset name' })
    const save = screen.getByRole('button', { name: 'Save here' })
    expect(input).toHaveValue(' padded.parquet ')
    expect(screen.getByRole('alert')).toHaveTextContent(/surrounding whitespace/i)
    expect(save).toBeDisabled()
    fireEvent.click(save)
    expect(pick).not.toHaveBeenCalled()

    for (const [invalid, message] of [
      ['../outside', /one name, not a path/i],
      ['..', /only of dots/i],
      ['line\u0085break', /control characters/i],
    ] as const) {
      fireEvent.change(input, { target: { value: invalid } })
      expect(screen.getByRole('alert')).toHaveTextContent(message)
      expect(save).toBeDisabled()
      fireEvent.click(save)
      expect(pick).not.toHaveBeenCalled()
    }

    fireEvent.change(input, { target: { value: 'padded.parquet' } })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(save).toBeEnabled()
    fireEvent.click(save)
    expect(pick).toHaveBeenCalledWith({
      destId: 'local', destName: 'Workspace', path: '', filename: 'padded.parquet',
    })
  })

  it('creates and enters a child folder before saving', async () => {
    const pick = vi.fn()
    render(<FileDialog mode="save" defaultName="embeddings.parquet" onClose={vi.fn()} onPick={pick} />)

    await screen.findByText('orders.csv')
    fireEvent.click(screen.getByRole('button', { name: 'New folder' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'New folder name' }), { target: { value: 'experiments' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(mocks.mkdirDestination).toHaveBeenCalledWith('local', '', 'experiments'))
    await waitFor(() => expect(mocks.browseDestination).toHaveBeenCalledWith('local', 'experiments'))
    expect(screen.getByRole('button', { name: 'experiments' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Save here' }))
    expect(pick).toHaveBeenCalledWith({
      destId: 'local', destName: 'Workspace', path: 'experiments', filename: 'embeddings.parquet',
    })
  })

  it('uses the backend-resolved folder instead of retaining a stale requested path', async () => {
    mocks.browseDestination.mockImplementation(async (_destinationId: string, path: string) => (
      path === 'alias'
        ? { path: '', entries: [], writable: true }
        : {
            path: '',
            entries: [{ name: 'alias', kind: 'dir' as const, uri: 'file:///data/alias' }],
            writable: true,
          }
    ))
    const pick = vi.fn()
    render(<FileDialog mode="save" defaultName="results" onClose={vi.fn()} onPick={pick} />)

    fireEvent.click(await screen.findByRole('button', { name: 'alias' }))
    await waitFor(() => expect(mocks.browseDestination).toHaveBeenCalledWith('local', 'alias'))
    await waitFor(() => expect(mocks.browseDestination).toHaveBeenLastCalledWith('local', ''))
    fireEvent.click(screen.getByRole('button', { name: 'Save here' }))

    expect(pick).toHaveBeenCalledWith({
      destId: 'local', destName: 'Workspace', path: '', filename: 'results',
    })
  })

  it('keeps invalid-name and create-folder failures inline and recoverable', async () => {
    mocks.mkdirDestination
      .mockResolvedValueOnce({ error: 'folder already exists' })
      .mockResolvedValueOnce({ ok: true })
    render(<FileDialog mode="save" defaultName="results" onClose={vi.fn()} onPick={vi.fn()} />)

    await screen.findByText('orders.csv')
    fireEvent.click(screen.getByRole('button', { name: 'New folder' }))
    const input = screen.getByRole('textbox', { name: 'New folder name' })
    fireEvent.change(input, { target: { value: ' padded ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText(/one exact folder name/i)).toBeVisible()
    expect(mocks.mkdirDestination).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: '../outside' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText(/one exact folder name/i)).toBeVisible()
    expect(mocks.mkdirDestination).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: 'daily' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText('folder already exists')).toBeVisible()
    expect(input).toHaveValue('daily')

    fireEvent.change(input, { target: { value: 'weekly' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(mocks.mkdirDestination).toHaveBeenCalledTimes(2))
    expect(await screen.findByRole('button', { name: 'weekly' })).toBeVisible()
  })

  it('keeps a list-denied configured prefix selectable and hands management to Settings', async () => {
    mocks.destinations.mockResolvedValueOnce({
      destinations: [
        { id: 's3-results', name: 'Research outputs', backend: 's3', root: 's3://ml-results/daily' },
      ],
      backends: ['local', 's3', 'gs'],
    })
    mocks.browseDestination.mockResolvedValueOnce({
      path: '', entries: [], writable: true, error: 'Access Denied while listing',
    })
    const manage = vi.fn()
    const pick = vi.fn()
    render(
      <FileDialog mode="save" defaultName="embeddings.parquet" onClose={vi.fn()}
        onPick={pick} onManageDestinations={manage} />,
    )

    expect(await screen.findByText(/Couldn't load this folder: Access Denied/i)).toBeVisible()
    expect(screen.getByText(/You can still save to this configured location/i)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save here' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Save here' }))
    expect(pick).toHaveBeenCalledWith({
      destId: 's3-results', destName: 'Research outputs', path: '', filename: 'embeddings.parquet',
    })

    fireEvent.click(screen.getByRole('button', { name: 'Manage destinations' }))
    expect(manage).toHaveBeenCalledTimes(1)
    expect(manage).toHaveBeenCalledWith({
      destId: 's3-results', path: '', filename: 'embeddings.parquet',
    }, expect.any(HTMLElement))
  })
})
