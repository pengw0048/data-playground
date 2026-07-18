import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => {
  const refreshFiles = vi.fn(async () => true)
  const pushToast = vi.fn()
  const state = {
    currentUser: { id: 'owner' } as { id: string } | null,
    doc: { id: 'source-canvas' },
    view: 'canvas',
    refreshFiles,
    openFile: vi.fn(async (id: string) => {
      state.doc = { id }
      state.view = 'canvas'
      return true
    }),
    pushToast,
  }
  return {
    validateNativeCanvasImport: vi.fn(),
    importNativeCanvas: vi.fn(),
    refreshFiles,
    openFile: state.openFile,
    pushToast,
    state,
    useStore: Object.assign((selector: (value: typeof state) => unknown) => selector(state), { getState: () => state }),
  }
})
const { validateNativeCanvasImport, importNativeCanvas, refreshFiles, openFile, pushToast, state: storeState } = mocks
vi.mock('../api/client', () => ({
  api: {
    validateNativeCanvasImport: (...args: unknown[]) => mocks.validateNativeCanvasImport(...args),
    importNativeCanvas: (...args: unknown[]) => mocks.importNativeCanvas(...args),
  },
}))

vi.mock('../store/graph', () => ({ useStore: mocks.useStore }))

import { NativeCanvasImportModal } from './NativeCanvasImportModal'

const checked = (overrides: Partial<Record<string, unknown>> = {}) => ({
  name: 'Imported canvas', nodeCount: 2, edgeCount: 1, requirements: [], parameters: [], diagnostics: [],
  canImport: true, requiresConfirmation: false, validationDigest: 'digest-123', ...overrides,
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function file(name = 'canvas.dp-canvas.json', json = '{"format":"native"}') {
  const value = new File([json], name, { type: 'application/json' })
  if (!value.text) Object.assign(value, { text: async () => json })
  return value
}

async function select(next = file()) {
  fireEvent.change(screen.getByLabelText(/choose native canvas/i), { target: { files: [next] } })
  await waitFor(() => expect(validateNativeCanvasImport).toHaveBeenCalled())
}

describe('NativeCanvasImportModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    storeState.currentUser = { id: 'owner' }
    storeState.doc = { id: 'source-canvas' }
    storeState.view = 'canvas'
    validateNativeCanvasImport.mockResolvedValue(checked())
    importNativeCanvas.mockResolvedValue({ ok: true, id: 'imported-canvas', created: true, replayed: false })
  })

  it('sends the canonical warning validation digest only after confirmation', async () => {
    render(<NativeCanvasImportModal onClose={vi.fn()} />)
    validateNativeCanvasImport.mockResolvedValue(checked({
      requiresConfirmation: true,
      diagnostics: [{ code: 'missing-data', severity: 'warning', message: 'Relink dataset' }],
      validationDigest: 'warning-digest',
    }))

    await select()
    expect(screen.getByText('Relink dataset')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /import as new canvas/i })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: /import as new canvas/i }))

    await waitFor(() => expect(importNativeCanvas).toHaveBeenCalledWith(
      expect.objectContaining({ validationDigest: 'warning-digest', confirmWarnings: true }),
      { signal: expect.any(AbortSignal) },
    ))
  })

  it('keeps one import id when a response-loss retry repeats the import', async () => {
    const responseLoss = new Error('The connection closed before a response arrived')
    importNativeCanvas.mockRejectedValue(responseLoss)
    render(<NativeCanvasImportModal onClose={vi.fn()} />)

    await select()
    fireEvent.click(screen.getByRole('button', { name: /import as new canvas/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('connection closed'))
    fireEvent.click(screen.getByRole('button', { name: /import as new canvas/i }))

    await waitFor(() => expect(importNativeCanvas).toHaveBeenCalledTimes(2))
    expect(importNativeCanvas.mock.calls[1][0]).toEqual(expect.objectContaining({
      importId: importNativeCanvas.mock.calls[0][0].importId,
      validationDigest: 'digest-123',
    }))
  })

  it('cancels a busy validation without deleting or opening anything', async () => {
    const pending = deferred<ReturnType<typeof checked>>()
    let signal: AbortSignal | undefined
    validateNativeCanvasImport.mockImplementation((_body, options) => {
      signal = (options as { signal?: AbortSignal })?.signal
      return pending.promise
    })
    const onClose = vi.fn()
    render(<NativeCanvasImportModal onClose={onClose} />)

    await select()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }))
    expect(signal?.aborted).toBe(true)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(openFile).not.toHaveBeenCalled()
    await act(async () => { pending.resolve(checked()); await pending.promise })
    expect(openFile).not.toHaveBeenCalled()
  })

  it('aborts a replaced file validation and ignores its late result', async () => {
    const first = deferred<ReturnType<typeof checked>>()
    let firstSignal: AbortSignal | undefined
    validateNativeCanvasImport.mockImplementationOnce((_body, options) => {
      firstSignal = (options as { signal?: AbortSignal })?.signal
      return first.promise
    }).mockResolvedValueOnce(checked({ name: 'Second canvas', validationDigest: 'second-digest' }))
    render(<NativeCanvasImportModal onClose={vi.fn()} />)

    await select(file('first.dp-canvas.json'))
    await select(file('second.dp-canvas.json'))
    expect(firstSignal?.aborted).toBe(true)
    await act(async () => { first.resolve(checked({ name: 'Late first canvas' })); await first.promise })

    expect(screen.getByText('Second canvas')).toBeInTheDocument()
    expect(screen.queryByText('Late first canvas')).not.toBeInTheDocument()
  })

  it('does not open a Canvas when a late import result follows navigation', async () => {
    const pending = deferred<{ ok: boolean; id: string; created: boolean; replayed: boolean }>()
    importNativeCanvas.mockReturnValue(pending.promise)
    const onClose = vi.fn()
    render(<NativeCanvasImportModal onClose={onClose} />)

    await select()
    fireEvent.click(screen.getByRole('button', { name: /import as new canvas/i }))
    await waitFor(() => expect(importNativeCanvas).toHaveBeenCalled())
    storeState.doc = { id: 'canvas-after-navigation' }
    await act(async () => { pending.resolve({ ok: true, id: 'late-import', created: true, replayed: false }); await pending.promise })

    expect(refreshFiles).not.toHaveBeenCalled()
    expect(openFile).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('allows only its exact destination transition and reports a successful reactive open', async () => {
    const onClose = vi.fn()
    const rendered = render(<NativeCanvasImportModal onClose={onClose} />)
    openFile.mockImplementationOnce(async (id: string) => {
      storeState.doc = { id }
      // Model the selector-driven rerender the real Zustand store performs inside openFile.
      rendered.rerender(<NativeCanvasImportModal onClose={onClose} />)
      return true
    })

    await select()
    fireEvent.click(screen.getByRole('button', { name: /import as new canvas/i }))

    await waitFor(() => expect(openFile).toHaveBeenCalledWith('imported-canvas'))
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith('Imported a new Canvas.', 'success'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
