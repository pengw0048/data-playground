import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  workspaceBrowse: vi.fn(), validateCanvasCopy: vi.fn(), createCanvasCopy: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mocks }))

import { useStore } from '../store/graph'
import { CanvasCopyModal } from './CanvasCopyModal'

const validation = (warnings = false) => ({
  name: 'Source copy', nodeCount: 2, edgeCount: 1, requirements: [], parameters: [],
  diagnostics: warnings ? [{ code: 'data_unavailable', severity: 'warning' as const, message: 'Relink data.' }] : [],
  canImport: true, requiresConfirmation: warnings,
  validationDigest: 'a'.repeat(64), copyIntentDigest: 'b'.repeat(64),
})

describe('CanvasCopyModal', () => {
  const refreshFiles = vi.fn()
  const openFile = vi.fn()
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.workspaceBrowse.mockResolvedValue({
      container: { id: 'container:workspace-local-root', kind: 'container', name: 'Workspace', version: 3, detached: false, source: 'local' },
      items: [], hasMore: false, completeness: 'complete', sources: [],
    })
    mocks.validateCanvasCopy.mockResolvedValue(validation())
    mocks.createCanvasCopy.mockResolvedValue({ ok: true, id: 'copied-canvas', created: true, replayed: false })
    refreshFiles.mockResolvedValue(true)
    openFile.mockResolvedValue(true)
    useStore.setState({
      currentUser: { id: 'viewer', name: 'Viewer' }, view: 'canvas',
      refreshFiles, openFile, pushToast: vi.fn(),
    } as never)
  })

  it('validates an exact current Canvas destination before creating and opening', async () => {
    const onClose = vi.fn()
    render(<CanvasCopyModal source={{ canvasId: 'source', version: 7, name: 'Research' }} onClose={onClose} />)
    expect(await screen.findByText(/Destination:/)).toHaveTextContent('Workspace')
    fireEvent.click(screen.getByRole('button', { name: 'Review copy' }))
    await screen.findByText('2 nodes · 1 connections · 0 requirements')
    const request = {
      sourceCanvasId: 'source', sourceCanvasVersion: 7,
      containerId: 'workspace-local-root', expectedContainerVersion: 3,
      name: 'Research copy',
    }
    expect(mocks.validateCanvasCopy).toHaveBeenCalledWith(expect.objectContaining(request))

    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    await waitFor(() => expect(openFile).toHaveBeenCalledWith('copied-canvas'))
    expect(mocks.createCanvasCopy).toHaveBeenCalledWith(expect.objectContaining({
      ...request,
      copyIntentDigest: 'b'.repeat(64), validationDigest: 'a'.repeat(64),
      confirmWarnings: false,
    }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('keeps one copy UUID across a lost-response retry and requires warning acknowledgement', async () => {
    mocks.validateCanvasCopy.mockResolvedValue(validation(true))
    mocks.createCanvasCopy.mockRejectedValueOnce(new Error('response lost')).mockResolvedValueOnce({
      ok: true, id: 'replayed-copy', created: false, replayed: true,
    })
    render(<CanvasCopyModal source={{ canvasId: 'source', subjectId: 't:task-1', name: 'Historical' }} onClose={() => {}} />)
    await screen.findByText(/Destination:/)
    fireEvent.click(screen.getByRole('button', { name: 'Review copy' }))
    expect(await screen.findByText('Relink data.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Create and open' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('response lost')
    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    await waitFor(() => expect(openFile).toHaveBeenCalledWith('replayed-copy'))
    const [first, second] = mocks.createCanvasCopy.mock.calls.map(([body]) => body)
    expect(first.copyId).toBe(second.copyId)
    expect(first.sourceSubjectId).toBe('t:task-1')
    expect(first.confirmWarnings).toBe(true)
  })

  it('cannot close after the atomic create request is submitted and opens its result', async () => {
    let resolveCreate!: (value: { ok: true, id: string, created: true, replayed: false }) => void
    mocks.createCanvasCopy.mockReturnValue(new Promise((resolve) => { resolveCreate = resolve }))
    const onClose = vi.fn()
    render(<CanvasCopyModal source={{ canvasId: 'source', version: 7, name: 'Research' }} onClose={onClose} />)
    await screen.findByText(/Destination:/)
    fireEvent.click(screen.getByRole('button', { name: 'Review copy' }))
    await screen.findByText('2 nodes · 1 connections · 0 requirements')

    fireEvent.click(screen.getByRole('button', { name: 'Create and open' }))
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Creating your Canvas… This request has been submitted and cannot be cancelled.')
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    const close = screen.getByRole('button', { name: 'Close' })
    expect(cancel).toBeDisabled()
    expect(close).toBeDisabled()
    fireEvent.click(cancel)
    fireEvent.click(close)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeVisible()

    resolveCreate({ ok: true, id: 'delayed-copy', created: true, replayed: false })
    await waitFor(() => expect(openFile).toHaveBeenCalledWith('delayed-copy'))
    expect(onClose).toHaveBeenCalledOnce()
  })
})
