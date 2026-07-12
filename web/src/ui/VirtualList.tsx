import { useEffect, useRef, useState } from 'react'

// A minimal fixed-row-height windowed list: renders only the visible rows (+ overscan) no matter how
// many items there are, so a list of thousands stays smooth. No dependency — the whole point is that
// "clone it and it works" keeps holding. Calls onEndReached near the bottom to drive infinite scroll.
export function VirtualList<T>({
  items, rowHeight, overscan = 8, renderRow, onEndReached, endThreshold = 400,
  className, style, emptyNote, resetKey,
}: {
  items: T[]
  rowHeight: number
  overscan?: number
  renderRow: (item: T, index: number) => React.ReactNode
  onEndReached?: () => void
  endThreshold?: number
  className?: string
  style?: React.CSSProperties
  emptyNote?: React.ReactNode
  resetKey?: unknown  // a new query: re-arm the end-reached guard + scroll back to the top
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [height, setHeight] = useState(0)
  const firedAt = useRef(-1)

  useEffect(() => {
    firedAt.current = -1
    setScrollTop(0)
    if (ref.current) ref.current.scrollTop = 0
  }, [resetKey])
  // new content (an appended page, or a replaced array after a retry) re-arms the guard
  useEffect(() => { firedAt.current = -1 }, [items])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    setHeight(el.clientHeight)
    const ro = new ResizeObserver(() => setHeight(el.clientHeight))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const total = items.length
  const totalHeight = total * rowHeight
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan)
  const visibleCount = Math.ceil((height || 600) / rowHeight) + overscan * 2
  const end = Math.min(total, start + visibleCount)

  const onScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget
    setScrollTop(el.scrollTop)
    if (!onEndReached) return
    const nearEnd = el.scrollHeight - el.scrollTop - el.clientHeight < endThreshold
    // fire once per end-zone entry per content set (re-entrancy is the PARENT's in-flight flag);
    // leaving the zone re-arms, so a failed page load can be retried by scrolling
    if (nearEnd) {
      if (firedAt.current !== total) { firedAt.current = total; onEndReached() }
    } else firedAt.current = -1
  }

  if (total === 0 && emptyNote) return <div ref={ref} className={className} style={style}>{emptyNote}</div>

  const slice = []
  for (let i = start; i < end; i++) {
    slice.push(
      <div key={i} style={{ position: 'absolute', top: i * rowHeight, left: 0, right: 0, height: rowHeight }}>
        {renderRow(items[i], i)}
      </div>,
    )
  }

  return (
    <div ref={ref} onScroll={onScroll} className={className} style={{ overflowY: 'auto', ...style }}>
      <div style={{ position: 'relative', height: totalHeight }}>{slice}</div>
    </div>
  )
}
