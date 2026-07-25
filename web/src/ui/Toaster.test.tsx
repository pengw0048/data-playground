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
}

vi.mock('../store/graph', () => ({ useStore: (selector: (value: typeof state) => unknown) => selector(state) }))

import { Toaster } from './Toaster'

describe('Toaster', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    state.toasts = []
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
})
