import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

// mock the network client + the store the modal pulls its actions from
const importPipeline = vi.fn()
vi.mock('../api/client', () => ({ api: { importPipeline: (...a: unknown[]) => importPipeline(...a) } }))

const newFile = vi.fn(async () => ({ ok: true as const, canvasId: 'fresh', persistence: 'remote' as const }))
const applyAgentGraph = vi.fn(() => true)
const pushToast = vi.fn()
vi.mock('../store/graph', () => ({
  useStore: (sel: (s: unknown) => unknown) => sel({ newFile, applyAgentGraph, pushToast }),
}))

import { ImportPipelineModal } from './ImportPipelineModal'

function typeConfig(text: string) {
  // fireEvent.change (not userEvent.type) so JSON braces aren't parsed as keyboard descriptors
  fireEvent.change(screen.getByPlaceholderText(/source/i), { target: { value: text } })
}
const importBtn = () => screen.getByRole('button', { name: /import/i })

describe('ImportPipelineModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    newFile.mockResolvedValue({ ok: true, canvasId: 'fresh', persistence: 'remote' })
    applyAgentGraph.mockReturnValue(true)
  })

  it('drops a returned graph onto a FRESH canvas (newFile before apply) and toasts success', async () => {
    const graph = { nodes: [{ id: 'src', type: 'source', position: { x: 0, y: 0 }, data: {} }], edges: [] }
    importPipeline.mockResolvedValue({ graph })
    const onClose = vi.fn()
    render(<ImportPipelineModal onClose={onClose} />)

    typeConfig('{"source":"x"}')
    fireEvent.click(importBtn())

    await waitFor(() => expect(applyAgentGraph).toHaveBeenCalledWith(graph, 'fresh'))
    expect(importPipeline).toHaveBeenCalledWith('{"source":"x"}')
    expect(newFile).toHaveBeenCalled()  // imported into a fresh file (applyAgentGraph REPLACES the canvas)
    expect(newFile.mock.invocationCallOrder[0]).toBeLessThan(applyAgentGraph.mock.invocationCallOrder[0])
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining('Imported'), 'success')
    expect(onClose).toHaveBeenCalled()
  })

  it('surfaces a missing-importer (501) as an error toast and does NOT touch the canvas', async () => {
    importPipeline.mockRejectedValue(new Error('No pipeline importer is registered'))
    render(<ImportPipelineModal onClose={vi.fn()} />)

    typeConfig('{"source":"x"}')
    fireEvent.click(importBtn())

    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.stringContaining('importer'), 'error'))
    expect(newFile).not.toHaveBeenCalled()
    expect(applyAgentGraph).not.toHaveBeenCalled()
  })

  it('info-toasts when the importer describes but returns no runnable graph', async () => {
    importPipeline.mockResolvedValue({})  // a description, no graph
    render(<ImportPipelineModal onClose={vi.fn()} />)

    typeConfig('{"source":"x"}')
    fireEvent.click(importBtn())

    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.stringContaining('no graph'), 'info'))
    expect(applyAgentGraph).not.toHaveBeenCalled()
  })

  it('does not replace the current graph when creating the import canvas is rejected', async () => {
    const graph = { nodes: [{ id: 'src', type: 'source', position: { x: 0, y: 0 }, data: {} }], edges: [] }
    importPipeline.mockResolvedValue({ graph })
    newFile.mockResolvedValue({ ok: false })
    const onClose = vi.fn()
    render(<ImportPipelineModal onClose={onClose} />)

    typeConfig('{"source":"x"}')
    fireEvent.click(importBtn())

    await waitFor(() => expect(newFile).toHaveBeenCalled())
    expect(applyAgentGraph).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalledWith(expect.stringContaining('Imported'), 'success')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not report a successful import when the created canvas is no longer active', async () => {
    const graph = { nodes: [{ id: 'src', type: 'source', position: { x: 0, y: 0 }, data: {} }], edges: [] }
    importPipeline.mockResolvedValue({ graph })
    applyAgentGraph.mockReturnValue(false)
    const onClose = vi.fn()
    render(<ImportPipelineModal onClose={onClose} />)

    typeConfig('{"source":"x"}')
    fireEvent.click(importBtn())

    await waitFor(() => expect(applyAgentGraph).toHaveBeenCalledWith(graph, 'fresh'))
    expect(pushToast).not.toHaveBeenCalledWith(expect.stringContaining('Imported'), 'success')
    expect(onClose).not.toHaveBeenCalled()
  })
})
