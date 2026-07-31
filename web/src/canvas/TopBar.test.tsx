import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const themeValues = new Map<string, string>()

const state = vi.hoisted(() => ({
  setJobsQuery: vi.fn(),
  setInboxQuery: vi.fn(),
  inboxQuery: '',
  newFile: vi.fn(),
  kernelInfo: { capabilities: [] as string[] },
  doc: { id: 'canvas-1', name: 'Quarterly customer acquisition and retention cohort analysis with regional attribution — July 2026 final review' },
  renameFile: vi.fn(),
  deleteFile: vi.fn(),
  discardLocalDraft: vi.fn(),
  currentDraftId: null,
  canvasRole: 'owner',
}))

vi.mock('../store/graph', () => ({
  roleCanEdit: (role: string | null) => role === 'owner' || role === 'editor',
  useStore: Object.assign(
    (selector: (value: typeof state) => unknown) => selector(state),
    { getState: () => state },
  ),
}))

import { AppMenu, CanvasOverflowMenu, CanvasTitle } from './TopBar'

beforeEach(() => {
  vi.clearAllMocks()
  themeValues.clear()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => themeValues.get(key) ?? null,
    setItem: (key: string, value: string) => { themeValues.set(key, value) },
    removeItem: (key: string) => { themeValues.delete(key) },
    clear: () => { themeValues.clear() },
  })
  state.canvasRole = 'owner'
  state.currentDraftId = null
  state.doc = {
    id: 'canvas-1',
    name: 'Quarterly customer acquisition and retention cohort analysis with regional attribution — July 2026 final review',
  }
  document.documentElement.removeAttribute('data-theme')
})

describe('AppMenu', () => {
  it('uses a standard menu affordance and keeps only global destinations and preferences', async () => {
    const user = userEvent.setup()
    render(
      <AppMenu
        onWorkspace={vi.fn()}
        onSettings={vi.fn()}
        onImport={vi.fn()}
        onNativeImport={vi.fn()}
      />,
    )

    const trigger = screen.getByRole('button', { name: 'Data Playground menu' })
    expect(trigger).toHaveAttribute('title', 'Data Playground menu')
    expect(trigger).not.toHaveTextContent('D')
    expect(trigger.querySelector('svg')).not.toBeNull()
    await user.click(trigger)

    for (const [label, detail] of [
      ['Jobs', 'runs and background tasks'],
      ['Inbox', 'my background task results'],
    ] as const) {
      expect(screen.getByRole('menuitem', { name: label })).toBeInTheDocument()
      const explanation = screen.getByText(detail)
      expect(explanation).toHaveAttribute('aria-hidden', 'true')
      expect(explanation).toHaveClass('whitespace-normal')
      expect(explanation).not.toHaveClass('truncate')
    }
    expect(screen.queryByText(/terminal outcomes/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/audit trail/i)).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Back to Workspace' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'New Canvas' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Import native Canvas…' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Appearance' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Run history' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Version history' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('copy-canvas')).not.toBeInTheDocument()
    expect(screen.queryByTestId('export-native-canvas')).not.toBeInTheDocument()
  })

  it('offers System, Light, and Dark as an Appearance submenu', async () => {
    const user = userEvent.setup()
    render(
      <AppMenu
        onWorkspace={vi.fn()}
        onSettings={vi.fn()}
        onImport={vi.fn()}
        onNativeImport={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Data Playground menu' }))
    await user.hover(screen.getByRole('menuitem', { name: 'Appearance' }))
    expect(await screen.findByRole('menuitemradio', { name: 'System' })).toHaveAttribute('data-state', 'checked')
    expect(screen.getByRole('menuitemradio', { name: 'Light' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Dark' }))
    await waitFor(() => expect(document.documentElement).toHaveAttribute('data-theme', 'dark'))
    expect(localStorage.getItem('dp-theme')).toBe('dark')
  })
})

describe('CanvasTitle', () => {
  it('edits in place, commits with Enter, and keeps the complete name discoverable', async () => {
    const user = userEvent.setup()
    render(<CanvasTitle />)

    const trigger = screen.getByTestId('canvas-title')
    expect(trigger).toHaveAttribute('title', `${state.doc.name} — click to rename`)
    expect(trigger).toHaveTextContent(state.doc.name)
    await user.click(trigger)
    const input = screen.getByRole('textbox', { name: 'Canvas name' })
    expect(input).toHaveValue(state.doc.name)
    await user.clear(input)
    await user.type(input, 'Renamed Canvas{Enter}')
    expect(state.renameFile).toHaveBeenLastCalledWith('Renamed Canvas')
    expect(screen.queryByRole('textbox', { name: 'Canvas name' })).not.toBeInTheDocument()
  })

  it('restores the original name with Escape', async () => {
    const user = userEvent.setup()
    render(<CanvasTitle />)

    await user.click(screen.getByTestId('canvas-title'))
    const input = screen.getByRole('textbox', { name: 'Canvas name' })
    await user.clear(input)
    await user.type(input, 'Discarded name{Escape}')
    expect(state.renameFile).toHaveBeenLastCalledWith(state.doc.name)
    expect(screen.queryByRole('textbox', { name: 'Canvas name' })).not.toBeInTheDocument()
  })

  it('commits the current name on blur', async () => {
    const user = userEvent.setup()
    render(<><CanvasTitle /><button type="button">Next control</button></>)

    await user.click(screen.getByTestId('canvas-title'))
    const input = screen.getByRole('textbox', { name: 'Canvas name' })
    await user.clear(input)
    await user.type(input, 'Blurred Canvas')
    await user.click(screen.getByRole('button', { name: 'Next control' }))
    expect(state.renameFile).toHaveBeenLastCalledWith('Blurred Canvas')
    expect(screen.queryByRole('textbox', { name: 'Canvas name' })).not.toBeInTheDocument()
  })

  it('does not enter edit mode for view-only access', async () => {
    state.canvasRole = 'viewer'
    render(<CanvasTitle />)

    const title = screen.getByTestId('canvas-title')
    expect(title).toBeDisabled()
    await userEvent.setup().click(title)
    expect(screen.queryByRole('textbox', { name: 'Canvas name' })).not.toBeInTheDocument()
    expect(state.renameFile).not.toHaveBeenCalled()
  })
})

describe('CanvasOverflowMenu', () => {
  it('owns only current-Canvas actions', async () => {
    const user = userEvent.setup()
    const onShowInWorkspace = vi.fn()
    render(
      <CanvasOverflowMenu
        onShowInWorkspace={onShowInWorkspace}
        onCanvasSettings={vi.fn()}
        onRunHistory={vi.fn()}
        onVersionHistory={vi.fn()}
        onNativeExport={vi.fn()}
        onCopy={vi.fn()}
        copyable
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Canvas actions' }))
    expect(screen.getByRole('menuitem', { name: 'Show in Workspace' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Canvas settings…' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Run history' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Version history' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Delete this Canvas' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'New Canvas' })).not.toBeInTheDocument()
    expect(screen.queryByText('Create example Canvas')).not.toBeInTheDocument()
    expect(screen.queryByText('Files')).not.toBeInTheDocument()
    await user.click(screen.getByRole('menuitem', { name: 'Show in Workspace' }))
    expect(onShowInWorkspace).toHaveBeenCalledTimes(1)
  })
})
