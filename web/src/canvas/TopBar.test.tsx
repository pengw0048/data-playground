import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  setJobsQuery: vi.fn(),
  setInboxQuery: vi.fn(),
  inboxQuery: '',
  newFile: vi.fn(),
  kernelInfo: { capabilities: [] as string[] },
}))

vi.mock('../store/graph', () => ({
  roleCanEdit: () => true,
  useStore: Object.assign(
    (selector: (value: typeof state) => unknown) => selector(state),
    { getState: () => state },
  ),
}))

import { AppMenu } from './TopBar'

describe('AppMenu', () => {
  it('describes work destinations in researcher language', async () => {
    const user = userEvent.setup()
    render(
      <AppMenu
        onWorkspace={vi.fn()}
        onSettings={vi.fn()}
        onRunHistory={vi.fn()}
        onVersionHistory={vi.fn()}
        onImport={vi.fn()}
        onNativeImport={vi.fn()}
        onNativeExport={vi.fn()}
        onCopy={vi.fn()}
        copyable
      />,
    )

    await user.click(screen.getByRole('button', { name: 'App menu' }))

    expect(screen.getByText('runs and background tasks')).toBeInTheDocument()
    expect(screen.getByText('my background task results')).toBeInTheDocument()
    expect(screen.getByText('runs from this Canvas')).toBeInTheDocument()
    expect(screen.queryByText(/terminal outcomes/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/audit trail/i)).not.toBeInTheDocument()
  })
})
