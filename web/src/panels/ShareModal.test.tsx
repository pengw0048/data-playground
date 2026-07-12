import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getShares: vi.fn(),
  addShare: vi.fn(),
  removeShare: vi.fn(),
  pushToast: vi.fn(),
  state: {
    doc: { id: 'canvas-1' },
    canvasRole: 'owner' as 'owner' | 'editor' | 'viewer' | null,
    users: [
      { id: 'alice', name: 'Alice' },
      { id: 'bob', name: 'Bob' },
      { id: 'casey', name: 'Casey' },
    ],
    currentUser: { id: 'alice', name: 'Alice' },
  },
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, getShares: mocks.getShares, addShare: mocks.addShare, removeShare: mocks.removeShare } }
})

vi.mock('../store/graph', () => ({
  useStore: (selector: (value: typeof mocks.state & { pushToast: typeof mocks.pushToast }) => unknown) => selector({ ...mocks.state, pushToast: mocks.pushToast }),
}))

import { ShareModal } from './ShareModal'

const httpError = (status: number, message: string) => Object.assign(new Error(message), { status })

describe('ShareModal — server-authoritative sharing truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.canvasRole = 'owner'
    mocks.state.currentUser = { id: 'alice', name: 'Alice' }
    mocks.getShares.mockResolvedValue({
      visibility: 'private',
      shares: [{ userId: 'bob', name: 'Bob', role: 'editor' }],
    })
    mocks.addShare.mockResolvedValue({ ok: true })
    mocks.removeShare.mockResolvedValue({ ok: true })
  })

  it('shows the actual viewer role and never labels that user as owner', async () => {
    mocks.state.canvasRole = 'viewer'
    mocks.state.currentUser = { id: 'casey', name: 'Casey' }
    mocks.getShares.mockResolvedValue({
      visibility: 'workspace_view',
      shares: [{ userId: 'casey', name: 'Casey', role: 'viewer' }],
    })

    render(<ShareModal onClose={vi.fn()} />)

    const currentUser = await screen.findByText(/Casey/)
    expect(currentUser).toHaveTextContent('can view')
    expect(currentUser).not.toHaveTextContent('owner')
    expect(screen.getByRole('button', { name: 'Everyone in workspace (view-only)' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Add' })).toBeNull()
  })

  it('keeps visibility unchanged on 403 and retries the exact mutation', async () => {
    mocks.addShare.mockRejectedValueOnce(httpError(403, 'forbidden')).mockResolvedValueOnce({ ok: true })
    render(<ShareModal onClose={vi.fn()} />)
    const workspace = await screen.findByRole('button', { name: 'Everyone in workspace' })
    const privateButton = screen.getByRole('button', { name: 'Private' })

    fireEvent.click(workspace)
    expect(await screen.findByRole('alert')).toHaveTextContent('403')
    expect(privateButton).toHaveAttribute('aria-pressed', 'true')
    expect(workspace).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(workspace).toHaveAttribute('aria-pressed', 'true'))
    expect(mocks.addShare).toHaveBeenNthCalledWith(2, 'canvas-1', { visibility: 'workspace' })
  })

  it('shows a pending state and blocks competing sharing mutations', async () => {
    let finish!: (value: { ok: boolean }) => void
    mocks.addShare.mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    render(<ShareModal onClose={vi.fn()} />)
    const workspace = await screen.findByRole('button', { name: 'Everyone in workspace' })

    fireEvent.click(workspace)

    expect(await screen.findByRole('status')).toHaveTextContent('Saving sharing changes')
    expect(screen.getByRole('button', { name: 'Private' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()

    finish({ ok: true })
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
    expect(workspace).toHaveAttribute('aria-pressed', 'true')
  })

  it('preserves the collaborator and role inputs after a 500', async () => {
    mocks.addShare.mockRejectedValueOnce(httpError(500, 'server exploded'))
    render(<ShareModal onClose={vi.fn()} />)
    const person = await screen.findByDisplayValue('Add a collaborator…')
    const access = screen.getByRole('combobox', { name: 'New collaborator access' }) as HTMLSelectElement

    fireEvent.change(person, { target: { value: 'casey' } })
    fireEvent.change(access, { target: { value: 'viewer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('500')
    expect(person).toHaveValue('casey')
    expect(access).toHaveValue('viewer')
    expect(screen.getByRole('button', { name: 'Add' })).toBeEnabled()
  })

  it('leaves the old collaborator role selected after a 401', async () => {
    mocks.addShare.mockRejectedValueOnce(httpError(401, 'session expired'))
    render(<ShareModal onClose={vi.fn()} />)
    const access = await screen.findByRole('combobox', { name: 'Access level for Bob' }) as HTMLSelectElement

    fireEvent.change(access, { target: { value: 'viewer' } })

    expect(await screen.findByRole('alert')).toHaveTextContent('401')
    expect(access).toHaveValue('editor')
  })

  it('keeps the collaborator row after an offline remove failure', async () => {
    mocks.removeShare.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    render(<ShareModal onClose={vi.fn()} />)
    await screen.findByText('Bob')

    fireEvent.click(screen.getByTitle('Remove'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to fetch')
    expect(screen.getByText('Bob')).toBeInTheDocument()
  })
})
