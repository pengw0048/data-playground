import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  fullscreenCode: null,
  canvasRole: 'owner',
  selectedIds: ['filter'],
  doc: { nodes: [{ id: 'filter', type: 'filter' }] },
  openPanels: {},
  undo: vi.fn(), redo: vi.fn(), select: vi.fn(), selectAll: vi.fn(),
  copySelection: vi.fn(), cutSelection: vi.fn(), paste: vi.fn(), duplicateSelected: vi.fn(),
}))
const commands = vi.hoisted(() => ({ remove: vi.fn(), bypass: vi.fn(), disable: vi.fn() }))
vi.mock('../store/graph', () => ({
  useStore: { getState: () => state, setState: vi.fn() },
  roleCanEdit: (role: string) => role === 'owner' || role === 'editor',
}))
vi.mock('../nodes/registry', () => ({ getSpec: () => ({ canBypass: true }) }))

import { useCanvasShortcuts } from './useCanvasShortcuts'

function Harness() {
  useCanvasShortcuts(commands.remove, commands.bypass, commands.disable)
  return <>
    <div tabIndex={0} aria-label="Canvas selection">Selected filter</div>
    <label>Write mode<select defaultValue="append"><option>append</option><option>overwrite</option></select></label>
    <button role="combobox" aria-label="Node parameter">Choose mode</button>
    <div role="listbox" aria-label="Node choices" tabIndex={0}><span>choice</span></div>
    <div role="menu" aria-label="Node actions" tabIndex={0}><span>action</span></div>
    <div role="dialog" aria-label="Node dialog" tabIndex={0}>dialog</div>
    <div tabIndex={0} aria-label="Handled shortcut" onKeyDown={(event) => event.preventDefault()}>handled</div>
  </>
}

const expectNoMutation = () => {
  expect(commands.remove).not.toHaveBeenCalled()
  expect(commands.bypass).not.toHaveBeenCalled()
  expect(commands.disable).not.toHaveBeenCalled()
  expect(state.undo).not.toHaveBeenCalled()
}

describe('Canvas keyboard ownership', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    state.canvasRole = 'owner'
  })

  it('does not edit the selected filter while typing into another node native select', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByLabelText('Write mode'))
    await user.keyboard('bd{Delete}{Control>}z{/Control}')
    expect(screen.getByLabelText('Write mode')).toHaveFocus()
    expectNoMutation()
  })

  it.each(['Node parameter', 'Node choices', 'Node actions', 'Node dialog'])(
    'lets %s own its keyboard input while a graph node stays selected', async (label) => {
      const user = userEvent.setup()
      render(<Harness />)
      await user.click(screen.getByLabelText(label))
      await user.keyboard('bd{Delete}')
      expectNoMutation()
    },
  )

  it('respects a key already handled by another surface and IME composition', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByLabelText('Handled shortcut'))
    await user.keyboard('bd{Delete}')
    fireEvent.keyDown(screen.getByLabelText('Canvas selection'), { key: 'b', isComposing: true })
    expectNoMutation()
  })

  it('keeps graph commands working on the Canvas and preserves the view-only boundary', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByLabelText('Canvas selection'))
    await user.keyboard('bd{Delete}{Control>}z{/Control}')
    expect(commands.bypass).toHaveBeenCalledExactlyOnceWith('filter')
    expect(commands.disable).toHaveBeenCalledExactlyOnceWith('filter')
    expect(commands.remove).toHaveBeenCalledOnce()
    expect(state.undo).toHaveBeenCalledOnce()
    vi.clearAllMocks()
    state.canvasRole = 'viewer'
    await user.keyboard('bd{Delete}{Control>}z{/Control}')
    expectNoMutation()
  })
})
