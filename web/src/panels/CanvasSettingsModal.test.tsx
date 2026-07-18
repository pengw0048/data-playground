import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getShares: vi.fn(),
  addShare: vi.fn(),
  state: {
    doc: { id: 'canvas-1', name: 'Revenue canvas', requirements: ['pandas'], parameters: [] as any[] },
    canvasRole: 'owner' as 'owner' | 'editor' | 'viewer' | null,
    renameFile: vi.fn(),
    setRequirements: vi.fn(),
    setParameters: vi.fn(),
  },
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, getShares: mocks.getShares, addShare: mocks.addShare } }
})

vi.mock('../store/graph', () => ({
  roleCanEdit: (role: string | null) => role === 'owner' || role === 'editor',
  useStore: (selector: (value: typeof mocks.state) => unknown) => selector(mocks.state),
}))

import { CanvasSettingsModal } from './CanvasSettingsModal'

describe('CanvasSettingsModal — sharing and read-only truth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.canvasRole = 'owner'
    mocks.getShares.mockResolvedValue({ visibility: 'private', shares: [] })
    mocks.addShare.mockResolvedValue({ ok: true })
    mocks.state.doc.parameters = []
  })

  it('renders workspace_view accurately and disables document fields for a viewer', async () => {
    mocks.state.canvasRole = 'viewer'
    mocks.getShares.mockResolvedValue({ visibility: 'workspace_view', shares: [] })
    render(<CanvasSettingsModal onClose={vi.fn()} />)

    expect(await screen.findByText('View-only access')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Revenue canvas')).toBeDisabled()
    expect(screen.getByDisplayValue('pandas')).toBeDisabled()
    const viewOnly = screen.getByRole('button', { name: /Workspace view-only/i })
    expect(viewOnly).toHaveAttribute('aria-pressed', 'true')
    expect(viewOnly).toBeDisabled()

    expect(mocks.state.renameFile).not.toHaveBeenCalled()
    expect(mocks.addShare).not.toHaveBeenCalled()
  })

  it('keeps the prior visibility on an offline failure and exposes Retry', async () => {
    mocks.addShare.mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce({ ok: true })
    render(<CanvasSettingsModal onClose={vi.fn()} />)
    const workspace = await screen.findByRole('button', { name: /^Workspace Everyone/i })
    const privateButton = screen.getByRole('button', { name: /^Private Only/i })

    fireEvent.click(workspace)
    expect(await screen.findByRole('alert')).toHaveTextContent('offline')
    expect(privateButton).toHaveAttribute('aria-pressed', 'true')
    expect(workspace).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(workspace).toHaveAttribute('aria-pressed', 'true'))
    expect(mocks.addShare).toHaveBeenNthCalledWith(2, 'canvas-1', { visibility: 'workspace' })
  })

  it('keeps invalid declaration edits local and validates dates and SecretRefs strictly', async () => {
    render(<CanvasSettingsModal onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add parameter' }))
    expect(mocks.state.setParameters).toHaveBeenCalledTimes(1)

    const name = screen.getByLabelText('Parameter name')
    fireEvent.change(name, { target: { value: '1bad' } })
    expect(screen.getByRole('alert')).toHaveTextContent('Names start with a letter')
    expect(mocks.state.setParameters).toHaveBeenCalledTimes(1)

    fireEvent.change(name, { target: { value: 'public_value' } })
    fireEvent.click(screen.getByLabelText('public_value type'))
    fireEvent.change(screen.getByLabelText('public_value type'), { target: { value: 'date' } })
    fireEvent.click(screen.getByLabelText('Required'))
    fireEvent.click(screen.getByLabelText('Default'))
    fireEvent.change(screen.getByLabelText('public_value default'), { target: { value: '2026-02-30' } })
    expect(screen.getByRole('alert')).toHaveTextContent('real YYYY-MM-DD')

    fireEvent.change(screen.getByLabelText('public_value type'), { target: { value: 'string' } })
    fireEvent.click(screen.getByLabelText('Required'))
    fireEvent.click(screen.getByLabelText('Default'))
    fireEvent.change(screen.getByLabelText('public_value default'), { target: { value: 'env:PRIVATE' } })
    expect(screen.getByRole('alert')).toHaveTextContent('SecretRef')
    fireEvent.change(screen.getByLabelText('public_value default'), { target: { value: 'FILE:/private/token' } })
    expect(screen.getByRole('alert')).toHaveTextContent('SecretRef')

    fireEvent.change(screen.getByLabelText('public_value default'), { target: { value: 's3://public-bucket/key' } })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
