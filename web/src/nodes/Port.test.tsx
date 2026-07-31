import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  canvasRole: 'owner',
  selectedIds: ['source-1'],
  doc: { edges: [] as Array<{ source?: string; sourceHandle?: string; target?: string; targetHandle?: string }> },
}))

vi.mock('@xyflow/react', () => ({
  Handle: ({ children, isConnectable: _isConnectable, ...props }: React.HTMLAttributes<HTMLDivElement> & { isConnectable?: boolean }) => (
    <div {...props}>{children}</div>
  ),
  Position: { Left: 'left', Right: 'right' },
}))

vi.mock('../store/graph', () => ({
  useStore: (selector: (value: typeof state) => unknown) => selector(state),
  roleCanEdit: (role: string) => role === 'owner' || role === 'editor',
}))

import { Port } from './Port'

describe('Port add affordance', () => {
  beforeEach(() => {
    state.canvasRole = 'owner'
    state.selectedIds = ['source-1']
    state.doc.edges = []
  })

  it('reveals the plus only on hover or focus and opens its picker from the keyboard', () => {
    const opened = vi.fn()
    window.addEventListener('dp-port-click', opened)
    render(<Port
      spec={{ id: 'out', wire: 'dataset' }}
      side="output"
      index={0}
      count={1}
      nodeId="source-1"
    />)

    const port = screen.getByRole('button', { name: 'Add operation from dataset output' })
    expect(port).not.toHaveTextContent('+')
    fireEvent.mouseEnter(port)
    expect(port).toHaveTextContent('+')
    fireEvent.mouseLeave(port)
    expect(port).not.toHaveTextContent('+')
    fireEvent.focus(port)
    expect(port).toHaveTextContent('+')
    fireEvent.mouseEnter(port)
    fireEvent.mouseLeave(port)
    expect(port).toHaveTextContent('+')
    vi.spyOn(port, 'getBoundingClientRect').mockReturnValue({
      left: 200, right: 215, top: 100, bottom: 115, width: 15, height: 15,
      x: 200, y: 100, toJSON: () => ({}),
    })

    fireEvent.keyDown(port, { key: 'Enter' })
    expect(opened).toHaveBeenCalledOnce()
    expect((opened.mock.calls[0][0] as CustomEvent).detail).toMatchObject({
      nodeId: 'source-1',
      handleId: 'out',
      opener: port,
      anchor: { left: 200, right: 215, top: 100, bottom: 115 },
    })
    fireEvent.blur(port)
    expect(port).not.toHaveTextContent('+')
    fireEvent.mouseEnter(port)
    fireEvent.focus(port)
    fireEvent.blur(port)
    expect(port).toHaveTextContent('+')
    fireEvent.mouseLeave(port)
    expect(port).not.toHaveTextContent('+')
    window.removeEventListener('dp-port-click', opened)
  })

  it('keeps inputs and view-only outputs out of the add-button tab order', () => {
    const { rerender } = render(<Port
      spec={{ id: 'in', wire: 'dataset' }}
      side="input"
      index={0}
      count={1}
      nodeId="source-1"
    />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()

    state.canvasRole = 'viewer'
    rerender(<Port
      spec={{ id: 'out', wire: 'dataset' }}
      side="output"
      index={0}
      count={1}
      nodeId="source-1"
    />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
