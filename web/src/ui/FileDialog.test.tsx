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
})
