import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ openMediaCell: vi.fn() }))
vi.mock('../api/client', () => ({
  api: mocks,
  KernelError: class KernelError extends Error {
    status: number
    code?: string
    constructor(status: number, message: string, code?: string) {
      super(message); this.status = status; this.code = code
    }
  },
}))

import { KernelError } from '../api/client'
import { MediaCellRenderer } from './MediaCellRenderer'

const exact = (identity = [{ name: 'frame_id', arrowType: 'uint64' as const, value: '18446744073709551615' }]) => ({
  datasetId: 'dataset-1', revisionId: 'revision-1', identity,
  proofStatus: 'certified' as const, certificationSupported: true,
})
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  return { promise: new Promise<T>((next) => { resolve = next }), resolve }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  mocks.openMediaCell.mockReset()
})

describe('MediaCellRenderer', () => {
  it('renders public image and video values directly without an exact endpoint request', () => {
    const { rerender } = render(<MediaCellRenderer column="asset" value="https://example.test/frame.png" mediaKind="unknown" />)
    expect(screen.getByRole('img', { name: 'Media image' })).toHaveAttribute('src', 'https://example.test/frame.png')
    expect(mocks.openMediaCell).not.toHaveBeenCalled()

    rerender(<MediaCellRenderer column="asset" value="data:video/webm;base64,AAAA" mediaKind="unknown" />)
    const video = screen.getByLabelText('Media video')
    expect(video).toHaveAttribute('preload', 'metadata')
    expect(video).toHaveAttribute('playsinline')
    expect(video).toHaveAttribute('controls')
    expect(mocks.openMediaCell).not.toHaveBeenCalled()
  })

  it('uses the canonical 64-bit sidecar identity unchanged for an exact binary cell', async () => {
    const create = vi.fn(() => 'blob:media-1')
    vi.stubGlobal('URL', { ...URL, createObjectURL: create, revokeObjectURL: vi.fn() })
    mocks.openMediaCell.mockResolvedValue(new Blob(['png'], { type: 'image/png' }))
    const identity = [{ name: 'frame_id', arrowType: 'uint64' as const, value: '18446744073709551615' }]
    render(<MediaCellRenderer column="frame" value="<3 bytes>" exact={exact(identity)} />)

    await waitFor(() => expect(mocks.openMediaCell).toHaveBeenCalledOnce())
    expect(mocks.openMediaCell).toHaveBeenCalledWith(
      'dataset-1', 'revision-1', { identity, column: 'frame' }, expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(await screen.findByRole('img', { name: 'Media image' })).toHaveAttribute('src', 'blob:media-1')
  })

  it.each([
    [413, undefined, 'safe preview limit'],
    [415, undefined, 'unsupported image or video format'],
    [401, undefined, 'not authorized'],
    [403, undefined, 'not authorized'],
    [410, undefined, 'no longer available'],
    [409, 'media_cell_identity_unavailable', 'identity is no longer available'],
    [404, undefined, 'could not be found'],
    [409, 'media_cell_row_ambiguous', 'matches more than one row'],
    [422, undefined, 'could not be found'],
    [503, undefined, 'media service failed'],
  ])('shows an explicit endpoint state for %s', async (status, code, text) => {
    mocks.openMediaCell.mockRejectedValue(new KernelError(status, 'kernel detail', code))
    render(<MediaCellRenderer column="frame" value="<3 bytes>" exact={exact()} />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(text))
  })

  it('reopens the existing certification action when the endpoint reports an unavailable identity', async () => {
    const open = vi.fn()
    mocks.openMediaCell.mockRejectedValue(new KernelError(409, 'kernel detail', 'media_cell_identity_unavailable'))
    render(<MediaCellRenderer column="frame" value="<3 bytes>" exact={{ ...exact(), onOpenCertification: open }} />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('identity is no longer available'))
    fireEvent.click(screen.getByRole('button', { name: 'Open certification' }))
    expect(open).toHaveBeenCalledOnce()
  })

  it('opens the existing certification action only for an exact supported but uncertified revision', () => {
    const open = vi.fn()
    render(<MediaCellRenderer column="frame" value="<3 bytes>" exact={{
      ...exact(), identity: null, proofStatus: 'unavailable', certificationSupported: true, onOpenCertification: open,
    }} />)
    expect(screen.getByRole('status')).toHaveTextContent('Certify row identity')
    fireEvent.click(screen.getByRole('button', { name: 'Open certification' }))
    expect(open).toHaveBeenCalledOnce()
  })

  it('does not request binary media from a generic surface', () => {
    render(<MediaCellRenderer column="frame" value="<3 bytes>" mediaKind="image" />)
    expect(screen.getByRole('status')).toHaveTextContent('Open an exact certified revision')
    expect(mocks.openMediaCell).not.toHaveBeenCalled()
  })

  it('waits for a near-visible exact cell before opening its bounded endpoint', async () => {
    let activate!: () => void
    class Observer {
      constructor(private callback: IntersectionObserverCallback) {
        activate = () => callback([{ isIntersecting: true } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
      }
      observe() {}
      disconnect() {}
      takeRecords() { return [] }
      root = null
      rootMargin = '0px'
      thresholds = []
    }
    vi.stubGlobal('IntersectionObserver', Observer)
    mocks.openMediaCell.mockResolvedValue(new Blob(['png'], { type: 'image/png' }))
    render(<MediaCellRenderer column="frame" value="<3 bytes>" exact={exact()} />)
    expect(mocks.openMediaCell).not.toHaveBeenCalled()
    activate()
    await waitFor(() => expect(mocks.openMediaCell).toHaveBeenCalledOnce())
  })

  it('shows an explicit load failure for a direct URL instead of retaining a broken element', () => {
    render(<MediaCellRenderer column="frame" value="https://example.test/frame.png" mediaKind="unknown" />)
    fireEvent.error(screen.getByRole('img', { name: 'Media image' }))
    expect(screen.getByRole('status')).toHaveTextContent('could not display')
  })

  it('does not suggest certification for an unclassifiable public URL', () => {
    render(<MediaCellRenderer column="frame" value="https://example.test/media" mediaKind="unknown" />)
    expect(screen.getByRole('status')).toHaveTextContent('public media URL is not a supported image or video')
    expect(mocks.openMediaCell).not.toHaveBeenCalled()
  })

  it('fences a stale response and revokes the current object URL after a display failure and unmount', async () => {
    const first = deferred<Blob>()
    const second = deferred<Blob>()
    mocks.openMediaCell.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const create = vi.fn(() => 'blob:current')
    const revoke = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL: create, revokeObjectURL: revoke })
    const { rerender, unmount } = render(<MediaCellRenderer column="frame" value="<3 bytes>" exact={exact()} />)
    const nextIdentity = [{ name: 'frame_id', arrowType: 'uint64' as const, value: '7' }]
    rerender(<MediaCellRenderer column="frame" value="<3 bytes>" exact={exact(nextIdentity)} />)

    first.resolve(new Blob(['old'], { type: 'image/png' }))
    await Promise.resolve()
    expect(create).not.toHaveBeenCalled()

    second.resolve(new Blob(['current'], { type: 'image/png' }))
    const image = await screen.findByRole('img', { name: 'Media image' })
    expect(create).toHaveBeenCalledOnce()
    fireEvent.error(image)
    expect(await screen.findByRole('status')).toHaveTextContent('could not display')
    expect(revoke).toHaveBeenCalledWith('blob:current')
    unmount()
    expect(revoke).toHaveBeenCalledTimes(1)
  })

  it('revokes an object URL on final unmount', async () => {
    const revoke = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:final'), revokeObjectURL: revoke })
    mocks.openMediaCell.mockResolvedValue(new Blob(['png'], { type: 'image/png' }))
    const { unmount } = render(<MediaCellRenderer column="frame" value="<3 bytes>" exact={exact()} />)
    await screen.findByRole('img', { name: 'Media image' })
    unmount()
    expect(revoke).toHaveBeenCalledWith('blob:final')
  })
})
