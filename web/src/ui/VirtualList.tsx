import { useEffect, useRef, useState } from 'react'

// A minimal fixed-row-height windowed list: renders only the visible rows (+ overscan) no matter how
// many items there are, so a list of thousands stays smooth. No dependency — the whole point is that
// "clone it and it works" keeps holding. Calls onEndReached near the bottom to drive infinite scroll.
export function VirtualList<T>({
  items, rowHeight, overscan = 8, renderRow, onEndReached, endThreshold = 400,
  className, style, emptyNote,
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
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [height, setHeight] = useState(0)
  const firedAt = useRef(-1)

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
    if (onEndReached && el.scrollHeight - el.scrollTop - el.clientHeight < endThreshold) {
      // guard so a burst of scroll events fires onEndReached once per new content length
      if (firedAt.current !== total) { firedAt.current = total; onEndReached() }
    }
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
