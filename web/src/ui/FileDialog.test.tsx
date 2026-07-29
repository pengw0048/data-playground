import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
    mocks.browseDestination.mockResolvedValue(BROWSE)
    mocks.mkdirDestination.mockResolvedValue({ ok: true })
  })
  afterEach(() => cleanup())

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

  it('keeps save selection capability-neutral until the chosen destination is admitted', async () => {
    mocks.destinations.mockResolvedValueOnce({
      destinations: [
        { id: 'managed', name: 'Workspace outputs', backend: 'local', root: '/outputs' },
        { id: 'external', name: 'External provider', backend: 'plugin', root: 'provider://exports' },
      ],
      backends: ['local', 'plugin'],
    })
    const pick = vi.fn()
    render(<FileDialog mode="save" defaultName="results" onClose={vi.fn()} onPick={pick} />)

    expect(await screen.findByText('Destinations')).toBeVisible()
    expect(screen.getByText('Choose output destination')).toBeVisible()
    expect(screen.getByLabelText('Selected destination')).toHaveTextContent('Workspace outputs')
    expect(screen.getByLabelText('Selected destination')).toHaveTextContent('Local')
    expect(screen.getByLabelText('Selected destination')).toHaveTextContent('/outputs')
    fireEvent.click(screen.getByRole('button', { name: 'External provider' }))
    expect(screen.getByLabelText('Selected destination')).toHaveTextContent('External provider')
    expect(screen.getByLabelText('Selected destination')).toHaveTextContent('plugin')
    expect(screen.getByLabelText('Selected destination')).toHaveTextContent('provider://exports')
    expect(screen.queryByText(/managed revision|versioned revision/i)).not.toBeInTheDocument()
    expect(screen.getByText('Output name')).toBeVisible()
    expect(screen.queryByText('orders.csv')).not.toBeInTheDocument()
    expect(screen.queryByTitle('New folder')).not.toBeInTheDocument()
    expect(mocks.browseDestination).not.toHaveBeenCalled()
    expect(mocks.mkdirDestination).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Use destination' }))

    expect(pick).toHaveBeenCalledWith({
      destId: 'external', destName: 'External provider', path: '', filename: 'results',
    })
  })

  it('explains object-store presets and hands destination management to Settings', async () => {
    mocks.destinations.mockResolvedValueOnce({
      destinations: [
        { id: 's3-results', name: 'Research outputs', backend: 's3', root: 's3://ml-results/daily' },
      ],
      backends: ['local', 's3', 'gs'],
    })
    const manage = vi.fn()
    render(
      <FileDialog mode="save" defaultName="embeddings.parquet" onClose={vi.fn()}
        onPick={vi.fn()} onManageDestinations={manage} />,
    )

    const selected = await screen.findByLabelText('Selected destination')
    expect(selected).toHaveTextContent('Research outputs')
    expect(selected).toHaveTextContent('S3')
    expect(selected).toHaveTextContent('s3://ml-results/daily')
    expect(selected).toHaveTextContent(/Add local, S3, or GCS locations as named destinations in Settings/i)

    fireEvent.click(screen.getByRole('button', { name: 'Manage destinations' }))
    expect(manage).toHaveBeenCalledTimes(1)
  })
})
