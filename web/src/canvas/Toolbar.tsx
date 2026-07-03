import { useRef, useState } from 'react'
import { useReactFlow } from '@xyflow/react'
import { allSpecs } from '../nodes'
import { useStore, freePosition } from '../store/graph'
import { categoryOrder, color, kindAccent, shadow, type Category } from '../theme/tokens'
import { Icon, type IconName } from '../ui/Icon'
import { Tooltip } from '../ui/Tooltip'
import { Popover } from '../ui/Popover'

const CATEGORY_ICON: Record<Category, IconName> = {
  io: 'db', shape: 'sample', compute: 'fx', query: 'sql', inspect: 'note', control: 'branch',
}
const CATEGORY_LABEL: Record<Category, string> = {
  io: 'Sources & sinks', shape: 'Shape', compute: 'Compute', query: 'Query', inspect: 'Inspect', control: 'Control flow',
}

// Bottom toolbar — auto-populated from the node registry, grouped by category (FR-C2a).
export function Toolbar() {
  const { screenToFlowPosition } = useReactFlow()
  const addNode = useStore((s) => s.addNode)
  const setAgentOpen = useStore((s) => s.setAgentOpen)
  const agentOpen = useStore((s) => s.agentOpen)
  const [open, setOpen] = useState<Category | null>(null)

  const specs = allSpecs()
  const cats = categoryOrder.filter((c) => specs.some((s) => s.category === c))

  const add = (kind: string) => {
    const c = screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 })
    const pos = freePosition(useStore.getState().doc.nodes, { x: c.x - 116, y: c.y - 40 })
    addNode(kind, pos)
    setOpen(null)
  }

  return (
    <div data-testid="toolbar" style={{ position: 'absolute', left: '50%', bottom: 22, transform: 'translateX(-50%)', zIndex: 16 }}>
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 4, padding: 6, background: '#fff',
          border: `1px solid ${color.border}`, borderRadius: 14, boxShadow: shadow.panel,
        }}
      >
        {cats.map((cat) => (
          <CategoryButton
            key={cat}
            cat={cat}
            open={open === cat}
            onToggle={() => setOpen((o) => (o === cat ? null : cat))}
            onClose={() => setOpen(null)}
            specs={specs.filter((s) => s.category === cat)}
            onPick={add}
          />
        ))}

        <div style={{ width: 1, height: 22, background: color.hairline, margin: '0 4px' }} />

        <button
          onClick={() => setAgentOpen(!agentOpen)}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 7, padding: '7px 14px', border: 'none', borderRadius: 10,
            background: agentOpen ? '#efeaff' : 'linear-gradient(180deg,#f3effe,#ece5fc)', color: '#6b4bd6', fontSize: 12.5, fontWeight: 600,
          }}
        >
          <Icon name="sparkle" size={14} /> Agent
        </button>
      </div>
    </div>
  )
}

function CategoryButton({ cat, open, onToggle, onClose, specs, onPick }: {
  cat: Category; open: boolean; onToggle: () => void; onClose: () => void
  specs: ReturnType<typeof allSpecs>; onPick: (kind: string) => void
}) {
  const ref = useRef<HTMLButtonElement>(null)
  return (
    <>
      <Tooltip label={CATEGORY_LABEL[cat]}>
        <button
          ref={ref}
          aria-label={CATEGORY_LABEL[cat]}
          onClick={(e) => { e.stopPropagation(); onToggle() }}
          style={{
            width: 38, height: 34, display: 'grid', placeItems: 'center', border: 'none', borderRadius: 10,
            background: open ? '#eef0f3' : 'transparent', color: open ? color.ink : color.text2,
          }}
        >
          <Icon name={CATEGORY_ICON[cat]} size={16} />
        </button>
      </Tooltip>
      {/* portal popover positioned once against the button (no percentage-based jump) */}
      <Popover anchorRef={ref} open={open} onClose={onClose} width={210} placement="top" align="left">
        <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3, padding: '5px 8px' }}>
          {CATEGORY_LABEL[cat]}
        </div>
        {specs.map((s) => (
          <button
            key={s.kind}
            onClick={(e) => { e.stopPropagation(); onPick(s.kind) }}
            style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%', textAlign: 'left', padding: '7px 8px', border: 'none', background: 'transparent', borderRadius: 7 }}
            onMouseEnter={(e) => (e.currentTarget.style.background = '#f2f3f5')}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
          >
            <span style={{ width: 4, height: 15, borderRadius: 2, background: kindAccent[s.kind] ?? color.text3 }} />
            <span style={{ display: 'flex', flexDirection: 'column' }}>
              <span style={{ fontSize: 12.5, fontWeight: 600, color: color.ink }}>{s.title}</span>
              <span style={{ fontSize: 10, color: color.text3 }}>{s.blurb}</span>
            </span>
          </button>
        ))}
      </Popover>
    </>
  )
}
