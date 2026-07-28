import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MediaCellRenderer } from './MediaCellRenderer'

afterEach(cleanup)

describe('MediaCellRenderer', () => {
  it('renders browser-displayable image and video values directly', () => {
    const { rerender } = render(
      <MediaCellRenderer column="asset" value="https://example.test/frame.png" mediaKind="unknown" />,
    )
    expect(screen.getByRole('img', { name: 'Media image' }))
      .toHaveAttribute('src', 'https://example.test/frame.png')

    rerender(
      <MediaCellRenderer column="asset" value="data:video/webm;base64,AAAA" mediaKind="unknown" />,
    )
    expect(screen.getByLabelText('Media video')).toHaveAttribute('preload', 'metadata')
  })

  it('replaces a failed direct load with a truthful state', () => {
    render(
      <MediaCellRenderer column="asset" value="https://example.test/missing.png" mediaKind="unknown" />,
    )

    fireEvent.error(screen.getByRole('img', { name: 'Media image' }))

    expect(screen.queryByRole('img', { name: 'Media image' })).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('browser could not display')
  })

  it('keeps byte-backed and empty values explicit without a recovery action', () => {
    const { rerender } = render(
      <MediaCellRenderer column="asset" value="<128 bytes>" mediaKind="image" />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Binary media preview is unavailable.')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()

    rerender(<MediaCellRenderer column="asset" value={null} mediaKind="image" />)
    expect(screen.getByRole('status')).toHaveTextContent('Media value is empty.')
  })
})
