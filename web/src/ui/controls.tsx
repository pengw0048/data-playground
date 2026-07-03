import type { CSSProperties, ReactNode } from 'react'
import { color, radius } from '../theme/tokens'

export function Segmented<T extends string>({ options, value, onChange, accent = color.focus }: {
  options: { value: T; label: string }[]
  value: T
  onChange: (v: T) => void
  accent?: string
}) {
  return (
    <div style={{ display: 'inline-flex', gap: 3, background: '#f1f2f4', padding: 2, borderRadius: radius.button }}>
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
    <label style={{ display: 'flex', flexDirection: 'column', gap: 3, ...style }}>
      <span style={{ fontSize: 9.5, fontWeight: 600, letterSpacing: 0.4, textTransform: 'uppercase', color: color.text3 }}>{label}</span>
      {children}
    </label>
  )
}

const inputBase: CSSProperties = {
  fontSize: 11.5, color: color.ink, background: '#fff', border: `1px solid ${color.border}`,
  borderRadius: 6, padding: '5px 7px', width: '100%', outline: 'none',
}

export function MiniInput({ value, onChange, placeholder, mono, onBlur }: {
  value: string; onChange: (v: string) => void; placeholder?: string; mono?: boolean; onBlur?: () => void
}) {
  return (
    <input
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      onBlur={onBlur}
      onClick={(e) => e.stopPropagation()}
      className={mono ? 'dp-mono' : undefined}
      style={{ ...inputBase, fontSize: mono ? 11 : 11.5 }}
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
      style={{ ...inputBase, appearance: 'none', cursor: 'pointer' }}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  )
}

export function Chip({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'blue' | 'amber' | 'green' }) {
  const tones = {
    neutral: { bg: '#f1f2f4', fg: color.text2 },
    blue: { bg: '#e7ecfb', fg: '#3355c6' },
    amber: { bg: '#fbf1dc', fg: '#a2731a' },
    green: { bg: '#e3f3ea', fg: '#1f7a45' },
  }[tone]
  return (
    <span style={{ fontSize: 10, fontWeight: 600, padding: '2px 7px', borderRadius: radius.chip, background: tones.bg, color: tones.fg }}>
      {children}
    </span>
  )
}
