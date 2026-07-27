import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../nodes/registry', () => ({
  kindsAcceptingWire: () => [{
    kind: 'long-name',
    title: 'A deliberately unbroken operation name that must not widen the menu',
    blurb: 'A deliberately unbroken description that must wrap inside the bounded connect menu.',
  }],
}))

import { ConnectMenu } from './ConnectMenu'

const originalViewport = { width: window.innerWidth, height: window.innerHeight }

function viewport(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
}

function menu() {
  const panel = screen.getByText('accepts dataset').closest('.dp-panel')
  if (!panel) throw new Error('connect menu was not rendered')
  return panel as HTMLDivElement
}

describe('ConnectMenu positioning', () => {
  afterEach(() => viewport(originalViewport.width, originalViewport.height))

  it('uses a bounded width and clamps a bottom-right port to the visible viewport', () => {
    viewport(1024, 780)
    render(<ConnectMenu x={1000} y={760} wire="dataset" onPick={vi.fn()} onFind={vi.fn()} onClose={vi.fn()} />)

    const panel = menu()
    expect(panel.style.width).toBe('300px')
    expect(panel.style.maxWidth).toBe('calc(100vw - 24px)')
    expect(panel.style.left).toBe('712px')
    expect(panel.style.top).toBe('448px')
  })

  it('keeps the menu within a narrow viewport and lets long candidate text wrap', () => {
    viewport(280, 200)
    render(<ConnectMenu x={270} y={190} wire="dataset" onPick={vi.fn()} onFind={vi.fn()} onClose={vi.fn()} />)

    const panel = menu()
    expect(panel.style.left).toBe('12px')
    expect(panel.style.top).toBe('12px')
    expect(panel.style.maxHeight).toBe('min(320px, calc(100vh - 24px))')
    expect(screen.getByText(/deliberately unbroken operation name/).style.overflowWrap).toBe('anywhere')
    expect(screen.getByText(/deliberately unbroken description/).style.overflowWrap).toBe('anywhere')
  })
})
