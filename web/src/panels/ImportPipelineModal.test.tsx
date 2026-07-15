import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

// mock the network client + the store the modal pulls its actions from
const importPipeline = vi.fn()
vi.mock('../api/client', () => ({ api: { importPipeline: (...a: unknown[]) => importPipeline(...a) } }))

const newFile = vi.fn(async (_options?: { signal?: AbortSignal }) => (
  { ok: true as const, canvasId: 'fresh', persistence: 'remote' as const }
))
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

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

function graph(id: string) {
  return { nodes: [{ id, type: 'source', position: { x: 0, y: 0 }, data: {} }], edges: [] }
}

describe('ImportPipelineModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    newFile.mockResolvedValue({ ok: true, canvasId: 'fresh', persistence: 'remote' })
    applyAgentGraph.mockReturnValue(true)
  })

  it('drops a returned graph onto a FRESH canvas (newFile before apply) and toasts success', async () => {
    const importedGraph = graph('src')
    importPipeline.mockResolvedValue({ graph: importedGraph })
    const onClose = vi.fn()
    render(<ImportPipelineModal onClose={onClose} />)

    typeConfig('{"source":"x"}')
    fireEvent.click(importBtn())

    await waitFor(() => expect(applyAgentGraph).toHaveBeenCalledWith(importedGraph, 'fresh'))
    expect(importPipeline).toHaveBeenCalledWith(
      '{"source":"x"}', undefined, { signal: expect.any(AbortSignal) },
    )
    expect(newFile).toHaveBeenCalledWith({ signal: expect.any(AbortSignal) })
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
    const importedGraph = graph('src')
    importPipeline.mockResolvedValue({ graph: importedGraph })
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
    const importedGraph = graph('src')
    importPipeline.mockResolvedValue({ graph: importedGraph })
    applyAgentGraph.mockReturnValue(false)
    const onClose = vi.fn()
    render(<ImportPipelineModal onClose={onClose} />)

    typeConfig('{"source":"x"}')
    fireEvent.click(importBtn())

    await waitFor(() => expect(applyAgentGraph).toHaveBeenCalledWith(importedGraph, 'fresh'))
    expect(pushToast).not.toHaveBeenCalledWith(expect.stringContaining('Imported'), 'success')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('aborts the importer and ignores its late result when Cancel closes the dialog', async () => {
    const pending = deferred<{ graph: ReturnType<typeof graph> }>()
    let signal: AbortSignal | undefined
    importPipeline.mockImplementationOnce((_config, _params, options) => {
      signal = (options as { signal?: AbortSignal })?.signal
      return pending.promise
    })
    const onClose = vi.fn()
    render(<ImportPipelineModal onClose={onClose} />)

    typeConfig('{"source":"slow"}')
    fireEvent.click(importBtn())
    await waitFor(() => expect(signal).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }))

    expect(signal?.aborted).toBe(true)
    expect(onClose).toHaveBeenCalledTimes(1)
    await act(async () => { pending.resolve({ graph: graph('late') }); await pending.promise })
    expect(newFile).not.toHaveBeenCalled()
    expect(applyAgentGraph).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('aborts the importer when Escape dismisses the dialog', async () => {
    const pending = deferred<{ graph: ReturnType<typeof graph> }>()
    let signal: AbortSignal | undefined
    importPipeline.mockImplementationOnce((_config, _params, options) => {
      signal = (options as { signal?: AbortSignal })?.signal
      return pending.promise
    })
    const onClose = vi.fn()
    render(<ImportPipelineModal onClose={onClose} />)

    typeConfig('{"source":"slow"}')
    fireEvent.click(importBtn())
    await waitFor(() => expect(signal).toBeDefined())
    fireEvent.keyDown(document, { key: 'Escape', code: 'Escape' })

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(signal?.aborted).toBe(true)
    await act(async () => { pending.resolve({ graph: graph('late') }); await pending.promise })
    expect(newFile).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('aborts and ignores late importer work when the dialog unmounts', async () => {
    const pending = deferred<{ graph: ReturnType<typeof graph> }>()
    let signal: AbortSignal | undefined
    importPipeline.mockImplementationOnce((_config, _params, options) => {
      signal = (options as { signal?: AbortSignal })?.signal
      return pending.promise
    })
    const view = render(<ImportPipelineModal onClose={vi.fn()} />)

    typeConfig('{"source":"slow"}')
    fireEvent.click(importBtn())
    await waitFor(() => expect(signal).toBeDefined())
    view.unmount()

    expect(signal?.aborted).toBe(true)
    await act(async () => { pending.resolve({ graph: graph('late') }); await pending.promise })
    expect(newFile).not.toHaveBeenCalled()
    expect(applyAgentGraph).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('invalidates destination creation when the dialog closes', async () => {
    const pending = deferred<{ ok: true; canvasId: string; persistence: 'remote' }>()
    let signal: AbortSignal | undefined
    importPipeline.mockResolvedValue({ graph: graph('src') })
    newFile.mockImplementationOnce((options) => {
      signal = options?.signal
      return pending.promise
    })
    const onClose = vi.fn()
    render(<ImportPipelineModal onClose={onClose} />)

    typeConfig('{"source":"x"}')
    fireEvent.click(importBtn())
    await waitFor(() => expect(newFile).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }))

    expect(signal?.aborted).toBe(true)
    await act(async () => {
      pending.resolve({ ok: true, canvasId: 'late-destination', persistence: 'remote' })
      await pending.promise
    })
    expect(applyAgentGraph).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('lets a newer import supersede an older generation during destination creation', async () => {
    const firstDestination = deferred<{ ok: true; canvasId: string; persistence: 'remote' }>()
    let firstSignal: AbortSignal | undefined
    importPipeline
      .mockResolvedValueOnce({ graph: graph('older') })
      .mockResolvedValueOnce({ graph: graph('newer') })
    newFile
      .mockImplementationOnce((options) => {
        firstSignal = options?.signal
        return firstDestination.promise
      })
      .mockResolvedValueOnce({ ok: true, canvasId: 'fresh', persistence: 'remote' })
    const onClose = vi.fn()
    render(<ImportPipelineModal onClose={onClose} />)

    typeConfig('{"source":"old"}')
    fireEvent.click(importBtn())
    await waitFor(() => expect(newFile).toHaveBeenCalledTimes(1))
    typeConfig('{"source":"new"}')
    fireEvent.click(screen.getByRole('button', { name: 'Restart import' }))

    await waitFor(() => expect(applyAgentGraph).toHaveBeenCalledWith(graph('newer'), 'fresh'))
    expect(firstSignal?.aborted).toBe(true)
    await act(async () => {
      firstDestination.resolve({ ok: true, canvasId: 'older-destination', persistence: 'remote' })
      await firstDestination.promise
    })
    expect(applyAgentGraph).toHaveBeenCalledTimes(1)
    expect(newFile).toHaveBeenCalledTimes(2)
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
