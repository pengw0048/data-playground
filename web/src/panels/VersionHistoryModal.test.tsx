import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listVersions: vi.fn(), restoreCanvas: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))

import { useStore } from '../store/graph'
import { VersionHistoryModal } from './VersionHistoryModal'

describe('VersionHistoryModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.listVersions.mockResolvedValue([
      { id: 'snapshot-13', version: 13, createdAt: '2026-08-03T14:15:00Z' },
      { id: 'snapshot-12', version: 12, label: 'Before restore', createdAt: null },
    ])
    useStore.setState({
      canvasRole: 'viewer',
      doc: {
        id: 'canvas-1', name: 'History test', version: 14,
        nodes: [], edges: [], requirements: [],
      },
    } as never)
  })

  it('identifies every disabled Restore action by snapshot version and recorded date', async () => {
    render(<VersionHistoryModal onClose={() => {}} />)

    const dated = await screen.findByRole('button', {
      name: /Restore snapshot v13 from .*2026/,
    })
    expect(dated).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Restore snapshot v12' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Restore' })).not.toBeInTheDocument()
  })

  it('keeps the target identity while that snapshot is being restored', async () => {
    mocks.restoreCanvas.mockReturnValue(new Promise(() => {}))
    useStore.setState({ canvasRole: 'owner' })
    render(<VersionHistoryModal onClose={() => {}} />)

    const restore = await screen.findByRole('button', {
      name: /Restore snapshot v13 from .*2026/,
    })
    fireEvent.click(restore)
    expect(await screen.findByRole('button', {
      name: /Restoring snapshot v13 from .*2026/,
    })).toBeDisabled()
  })
})
