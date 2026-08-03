import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const themeValues = new Map<string, string>()

const state = vi.hoisted(() => ({
  setJobsQuery: vi.fn(),
  newFile: vi.fn(),
  kernelUp: true,
  saved: true,
  kernelInfo: { capabilities: [] as string[], executionTargets: [] as Array<{ name: string; label: string; kind: 'interactive' | 'job'; description: string; substrate?: string }> },
  doc: { id: 'canvas-1', name: 'Quarterly customer acquisition and retention cohort analysis with regional attribution — July 2026 final review', nodes: [] as Array<{ data: { status?: string } }> } as { id: string; name: string; executionBackend?: string; nodes: Array<{ data: { status?: string } }> },
  authEnabled: false,
  setExecutionBackend: vi.fn(),
  renameFile: vi.fn(),
  deleteFile: vi.fn(),
  discardLocalDraft: vi.fn(),
  currentDraftId: null,
  localDrafts: [] as Array<{ draftId: string; syncState: string }>,
  canvasRole: 'owner',
  rerunAll: vi.fn(),
  cancelGraphRun: vi.fn(),
  graphRun: null,
  past: [] as unknown[],
  future: [] as unknown[],
  peers: {} as Record<string, { name: string; color: string }>,
}))

vi.mock('../store/graph', () => ({
  roleCanEdit: (role: string | null) => role === 'owner' || role === 'editor',
  useStore: Object.assign(
    (selector: (value: typeof state) => unknown) => selector(state),
    { getState: () => state },
  ),
}))

vi.mock('../panels/SettingsModal', () => ({
  SettingsModal: ({ onClose, initialCategory }: { onClose: () => void; initialCategory?: string }) => (
    <div role="dialog" aria-label="Settings">
      <span>{initialCategory}</span>
      <button type="button" onClick={onClose}>Close settings</button>
    </div>
  ),
}))
vi.mock('./CanvasWorkspaceLocation', () => ({ CanvasWorkspaceLocation: () => null }))
vi.mock('./CanvasInboxPopover', () => ({ CanvasInboxPopover: () => null }))
vi.mock('./KernelBadge', () => ({ KernelBadge: () => null }))

import { AppMenu, CanvasTitle, TopBar } from './TopBar'

const appMenuProps = () => ({
  onWorkspace: vi.fn(),
  onSettings: vi.fn(),
  onImport: vi.fn(),
  onNativeImport: vi.fn(),
  onCanvasSettings: vi.fn(),
  onRunHistory: vi.fn(),
  onVersionHistory: vi.fn(),
  onNativeExport: vi.fn(),
  onCopy: vi.fn(),
  copyable: true,
})

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
  state.localDrafts = []
  state.kernelInfo.executionTargets = []
  state.doc = {
    id: 'canvas-1',
    name: 'Quarterly customer acquisition and retention cohort analysis with regional attribution — July 2026 final review',
    nodes: [],
  }
  document.documentElement.removeAttribute('data-theme')
})

describe('AppMenu', () => {
  it('uses one standard menu for global destinations and current-Canvas actions', async () => {
    const user = userEvent.setup()
    const props = appMenuProps()
    render(<AppMenu {...props} />)

    const trigger = screen.getByRole('button', { name: 'Data Playground menu' })
    expect(trigger).toHaveAttribute('title', 'Data Playground menu')
    expect(trigger).not.toHaveTextContent('D')
    expect(trigger.querySelector('svg')).not.toBeNull()
    await user.click(trigger)

    expect(screen.queryByRole('menuitem', { name: 'Jobs' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Inbox' })).not.toBeInTheDocument()
    expect(screen.queryByText(/terminal outcomes/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/audit trail/i)).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Back to Workspace' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'New Canvas' })).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Import native Canvas…' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Appearance' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Canvas settings…' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Run history' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Version history' })).toBeInTheDocument()
    expect(screen.getByTestId('copy-canvas')).toBeInTheDocument()
    expect(screen.getByTestId('export-native-canvas')).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Delete this Canvas' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Canvas actions' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('menuitem', { name: 'Back to Workspace' }))
    expect(props.onWorkspace).toHaveBeenCalledTimes(1)
  })

  it('requires an in-app confirmation before deleting the current Canvas', async () => {
    const user = userEvent.setup()
    render(<AppMenu {...appMenuProps()} />)

    await user.click(screen.getByRole('button', { name: 'Data Playground menu' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete this Canvas' }))

    const dialog = await screen.findByRole('dialog', { name: /Delete .*July 2026 final review/ })
    expect(dialog).toHaveTextContent('This permanently deletes the Canvas')
    expect(state.deleteFile).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(state.deleteFile).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Data Playground menu' }))
    await user.click(screen.getByRole('menuitem', { name: 'Delete this Canvas' }))
    await user.click(await screen.findByRole('button', { name: 'Delete Canvas' }))

    expect(state.deleteFile).toHaveBeenCalledWith('canvas-1')
  })

  it('does not offer local-draft deletion while that draft is syncing', async () => {
    const user = userEvent.setup()
    state.currentDraftId = 'draft-1'
    state.localDrafts = [{ draftId: 'draft-1', syncState: 'syncing' }]
    render(<AppMenu {...appMenuProps()} />)

    await user.click(screen.getByRole('button', { name: 'Data Playground menu' }))
    const syncing = screen.getByRole('menuitem', { name: 'Syncing local draft…' })
    expect(syncing).toHaveAttribute('data-disabled')
    expect(syncing).toHaveAttribute('title', 'Wait for syncing to finish before deleting this draft')
    expect(screen.queryByRole('menuitem', { name: 'Delete this local draft' })).not.toBeInTheDocument()
  })

  it('offers System, Light, and Dark as an Appearance submenu', async () => {
    const user = userEvent.setup()
    render(<AppMenu {...appMenuProps()} />)

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

  it('drops an in-progress rename when browser navigation activates another Canvas', async () => {
    const user = userEvent.setup()
    state.doc = { id: 'canvas-a', name: 'Canvas A original', nodes: [] }
    const { rerender } = render(<CanvasTitle />)

    await user.click(screen.getByTestId('canvas-title'))
    const staleInput = screen.getByRole('textbox', { name: 'Canvas name' })
    await user.clear(staleInput)
    await user.type(staleInput, 'Canvas A in progress')

    state.doc = { id: 'canvas-b', name: 'Canvas B original', nodes: [] }
    state.renameFile.mockClear()
    fireEvent.change(staleInput, { target: { value: 'Late stale Canvas A edit' } })
    expect(state.renameFile).not.toHaveBeenCalled()
    rerender(<CanvasTitle />)

    expect(screen.queryByRole('textbox', { name: 'Canvas name' })).not.toBeInTheDocument()
    expect(screen.getByTestId('canvas-title')).toHaveTextContent('Canvas B original')
    fireEvent.keyDown(staleInput, { key: 'Escape' })
    expect(state.renameFile).not.toHaveBeenCalled()
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

describe('TopBar Settings handoff', () => {
  it('runs the scoped close callback after Settings restores its trigger', async () => {
    const onClose = vi.fn()
    render(<TopBar />)
    const trigger = screen.getByTestId('app-menu')

    fireEvent(window, new CustomEvent('dp-open-settings', {
      detail: { category: 'destinations', trigger, onClose },
    }))

    expect(screen.getByRole('dialog', { name: 'Settings' })).toHaveTextContent('destinations')
    fireEvent.click(screen.getByRole('button', { name: 'Close settings' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(trigger).toHaveFocus()
  })
})

describe('Canvas execution target', () => {
  it('starts the whole graph from the primary run control', async () => {
    render(<TopBar />)

    await userEvent.setup().click(screen.getByRole('button', { name: 'Run all' }))

    expect(state.rerunAll).toHaveBeenCalledTimes(1)
  })

  it('calls the primary action Rerun all after a prior attempt', async () => {
    state.doc.nodes = [{ data: { status: 'latest' } }]
    render(<TopBar />)

    await userEvent.setup().click(screen.getByRole('button', { name: 'Rerun all' }))

    expect(state.rerunAll).toHaveBeenCalledTimes(1)
  })

  it('selects a configured runner from the Canvas top bar', async () => {
    state.kernelInfo.executionTargets = [
      { name: 'kernel', label: 'Canvas worker', kind: 'interactive', description: 'Reusable worker.', substrate: 'pod' },
      { name: 'ray-data', label: 'Ray Jobs', kind: 'job', description: 'Submit a durable job.', substrate: 'ray-jobs' },
    ]
    render(<TopBar />)

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Execution target: Automatic' }))
    expect(screen.getByText('Run this Canvas on')).toBeVisible()
    await user.click(screen.getByRole('menuitemradio', { name: /Ray Jobs/ }))

    expect(state.setExecutionBackend).toHaveBeenCalledWith('ray-data')
  })
})

describe('Canvas top chrome', () => {
  it('does not capture pointer events outside its controls', () => {
    const { container } = render(<TopBar />)
    const band = container.querySelector<HTMLElement>('[data-layout-region="canvas-top-chrome"]')!

    expect(band.style.pointerEvents).toBe('none')
    expect(screen.getByTestId('canvas-run-controls').style.pointerEvents).toBe('auto')
    expect(screen.getByTestId('app-menu').closest<HTMLElement>('[style*="pointer-events"]')!.style.pointerEvents).toBe('auto')
  })
})
