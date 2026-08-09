import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = {
  toasts: [] as Array<{
    id: string
    kind: 'error' | 'info' | 'success'
    msg: string
    actions?: Array<{ label: string; onClick: () => void | Promise<unknown> }>
  }>,
  dismissToast: vi.fn(),
  kernelUp: true,
  accessDenied: false,
  bootstrap: vi.fn(),
  confirmDiscardDraftId: null as string | null,
  localDrafts: [] as Array<{ draftId: string; name: string }>,
  cancelDiscardLocalDraft: vi.fn(),
  discardLocalDraft: vi.fn(),
}

vi.mock('../store/graph', () => ({ useStore: (selector: (value: typeof state) => unknown) => selector(state) }))

import { Toaster } from './Toaster'

describe('Toaster', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    state.toasts = []
    state.confirmDiscardDraftId = null
    state.localDrafts = []
  })

  it('confirms a requested draft delete before discarding it', () => {
    state.confirmDiscardDraftId = 'draft-1'
    state.localDrafts = [{ draftId: 'draft-1', name: 'local edit' }]

    render(<Toaster />)
    expect(screen.getByText('Delete local draft “local edit”?')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Delete local draft' }))
    expect(state.cancelDiscardLocalDraft).toHaveBeenCalledOnce()
    expect(state.discardLocalDraft).toHaveBeenCalledWith('draft-1')
  })

  it('cancels a requested draft delete without discarding anything', () => {
    state.confirmDiscardDraftId = 'draft-1'
    state.localDrafts = [{ draftId: 'draft-1', name: 'local edit' }]

    render(<Toaster />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(state.cancelDiscardLocalDraft).toHaveBeenCalledOnce()
    expect(state.discardLocalDraft).not.toHaveBeenCalled()
  })

  it('renders a notification action and dismisses the notification after invoking it', () => {
    const recover = vi.fn()
    state.toasts = [{
      id: 'conflict',
      kind: 'error',
      msg: 'Your local draft is preserved.',
      actions: [{ label: 'Keep local draft as new Canvas', onClick: recover }],
    }]

    render(<Toaster />)
    fireEvent.click(screen.getByRole('button', { name: 'Keep local draft as new Canvas' }))

    expect(recover).toHaveBeenCalledOnce()
    expect(state.dismissToast).toHaveBeenCalledWith('conflict')
  })

  it('announces notifications from a live region that exists before the first one arrives', () => {
    const { container, rerender } = render(<Toaster />)
    const stack = container.querySelector('[aria-live="polite"][aria-atomic="false"]')
    expect(stack).not.toBeNull()

    state.toasts = [{ id: 'run', kind: 'error', msg: 'Run failed: column not found' }]
    rerender(<Toaster />)

    expect(stack).toContainElement(screen.getByTestId('toast'))
  })

  it('announces the offline banner', () => {
    state.kernelUp = false
    render(<Toaster />)

    expect(screen.getByRole('status')).toHaveTextContent('Data Playground is offline')
    state.kernelUp = true
  })
})
