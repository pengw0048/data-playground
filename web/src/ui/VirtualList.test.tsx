import { render, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { VirtualList } from './VirtualList'

const rows = (n: number) => Array.from({ length: n }, (_, i) => `row-${i}`)
const list = (items: string[], onEnd: () => void, resetKey: unknown) => (
  <VirtualList items={items} rowHeight={20} onEndReached={onEnd} resetKey={resetKey} renderRow={(x) => <span>{x}</span>} />
)

// jsdom does no layout, so scrollHeight - scrollTop - clientHeight === 0 < endThreshold: every scroll
// event counts as "near the end" — which is exactly what these guard tests need.
describe('VirtualList infinite-scroll guard', () => {
  it('fires onEndReached once per content set, and a resetKey change re-arms it + scrolls to top', () => {
    const onEnd = vi.fn()
    const { container, rerender } = render(list(rows(50), onEnd, 'a'))
    const el = container.firstElementChild as HTMLElement

    fireEvent.scroll(el)
    expect(onEnd).toHaveBeenCalledTimes(1)
    fireEvent.scroll(el) // same content length → guard holds
    expect(onEnd).toHaveBeenCalledTimes(1)

    el.scrollTop = 123
    rerender(list(rows(50), onEnd, 'b')) // same length, new query
    expect(el.scrollTop).toBe(0)
    fireEvent.scroll(el)
    expect(onEnd).toHaveBeenCalledTimes(2)
  })

  it('re-arms when the items array identity changes (a retried fetch can re-fire)', () => {
    const onEnd = vi.fn()
    const items = rows(50)
    const { container, rerender } = render(list(items, onEnd, 'a'))
    const el = container.firstElementChild as HTMLElement

    fireEvent.scroll(el)
    rerender(list(items, onEnd, 'a')) // same array identity → still armed against this content
    fireEvent.scroll(el)
    expect(onEnd).toHaveBeenCalledTimes(1)

    rerender(list(rows(50), onEnd, 'a')) // new array, same length (e.g. a retry replaced the page)
    fireEvent.scroll(el)
    expect(onEnd).toHaveBeenCalledTimes(2)
  })
})
