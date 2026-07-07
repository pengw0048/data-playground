import type { CSSProperties, ReactNode } from 'react'
import { color, radius } from '../theme/tokens'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/input'

export function Segmented<T extends string>({ options, value, onChange, accent = color.focus }: {
  options: { value: T; label: string }[]
  value: T
  onChange: (v: T) => void
  accent?: string
}) {
  return (
    <div style={{ display: 'inline-flex', gap: 3, background: 'hsl(var(--secondary))', padding: 2, borderRadius: radius.button }}>
      {options.map((o) => {
        const active = o.value === value
        return (
          <button
            key={o.value}
            onClick={(e) => { e.stopPropagation(); onChange(o.value) }}
            style={{
              fontSize: 11, fontWeight: 600, padding: '3px 9px', border: 'none', borderRadius: 6,
              background: active ? accent : 'transparent',
              color: active ? '#fff' : color.text2, cursor: 'pointer',
            }}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

export function Field({ label, children, style }: { label: string; children: ReactNode; style?: CSSProperties }) {
  return (
    <label className="flex flex-col gap-[3px]" style={style}>
      <span className="text-[9.5px] font-semibold uppercase tracking-[0.4px] text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}

// Compact overrides for the shadcn <Input> so it fits inside 232px node cards (the primitive's
// default h-9/px-3/text-sm is too tall). Also reused by ColumnCombo in nodes/fields.tsx.
export const miniInputClass = 'h-7 px-2 py-1 text-[11.5px] md:text-[11.5px] text-foreground shadow-none'
// A native <select> styled to match the shadcn Input look (Radix Select would change the DOM the
// E2E suite selects on). Kept as a plain <select> so behavior/emitted values are unchanged.
export const miniSelectClass =
  'h-7 w-full cursor-pointer rounded-md border border-input bg-transparent px-2 text-[11.5px] text-foreground outline-none focus:ring-1 focus:ring-ring focus:ring-offset-0'

export function MiniInput({ value, onChange, placeholder, mono, onBlur }: {
  value: string; onChange: (v: string) => void; placeholder?: string; mono?: boolean; onBlur?: () => void
}) {
  return (
    <Input
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      onBlur={onBlur}
      onClick={(e) => e.stopPropagation()}
      className={cn(miniInputClass, mono && 'dp-mono text-[11px] md:text-[11px]')}
    />
  )
}

export function MiniSelect<T extends string>({ value, options, onChange }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as T)}
      onClick={(e) => e.stopPropagation()}
      className={cn(miniSelectClass, 'appearance-none')}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  )
}

export function Chip({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'blue' | 'amber' | 'green' }) {
  // dual-theme tones (were light-only hex — the neutral one went light-on-light in dark mode)
  const cls = {
    neutral: 'bg-muted text-muted-foreground',
    blue: 'bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300',
    amber: 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
    green: 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300',
  }[tone]
  return <span className={cn('rounded px-[7px] py-0.5 text-[10px] font-semibold', cls)}>{children}</span>
}
