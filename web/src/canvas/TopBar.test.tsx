import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  setJobsQuery: vi.fn(),
  setInboxQuery: vi.fn(),
  inboxQuery: '',
  newFile: vi.fn(),
  kernelInfo: { capabilities: [] as string[] },
  doc: { id: 'canvas-1', name: 'Quarterly customer acquisition and retention cohort analysis with regional attribution — July 2026 final review' },
  files: [],
  openFile: vi.fn(),
  newFromExample: vi.fn(),
  renameFile: vi.fn(),
  deleteFile: vi.fn(),
  discardLocalDraft: vi.fn(),
  currentDraftId: null,
  canvasRole: 'owner',
}))

vi.mock('../store/graph', () => ({
  roleCanEdit: () => true,
  useStore: Object.assign(
    (selector: (value: typeof state) => unknown) => selector(state),
    { getState: () => state },
  ),
}))

import { AppMenu, FileMenu } from './TopBar'

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

    for (const [label, detail] of [
      ['Jobs', 'runs and background tasks'],
      ['Inbox', 'my background task results'],
      ['Run history', 'runs from this Canvas'],
    ] as const) {
      expect(screen.getByRole('menuitem', { name: label })).toBeInTheDocument()
      const explanation = screen.getByText(detail)
      expect(explanation).toHaveAttribute('aria-hidden', 'true')
      expect(explanation).toHaveClass('whitespace-normal')
      expect(explanation).not.toHaveClass('truncate')
    }
    expect(screen.queryByText(/terminal outcomes/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/audit trail/i)).not.toBeInTheDocument()
  })
})

describe('FileMenu', () => {
  it('keeps the complete Canvas name directly discoverable when the visible label truncates', () => {
    render(<FileMenu onCanvasSettings={vi.fn()} />)

    const trigger = screen.getByTestId('file-menu')
    expect(trigger).toHaveAttribute('title', state.doc.name)
    expect(trigger).toHaveTextContent(state.doc.name)
    expect(trigger.querySelector('.truncate')).not.toBeNull()
  })
})
