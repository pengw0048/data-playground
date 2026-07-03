import { useEffect, useState } from 'react'
import { useStore } from '../store/graph'
import { api } from '../api/client'
import { color } from '../theme/tokens'
import { Icon } from '../ui/Icon'
import type { LineageResult } from '../types/api'

// Trace parents/children of the dataset a node reads or writes (§5.9 / FR-L2).
export function LineagePanel({ nodeId }: { nodeId: string }) {
  const uri = useStore((s) => resolveUri(s.doc, nodeId))
  const [lin, setLin] = useState<LineageResult | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!uri) { setErr('this node has no registered dataset yet — run it first'); return }
    api.lineage(uri).then(setLin).catch((e) => setErr(e.message))
  }, [uri])

  if (err) return <div style={{ padding: 16, fontSize: 12, color: color.text3 }}>{err}</div>
  if (!lin) return <div style={{ padding: 16, fontSize: 12, color: color.text3 }}>tracing lineage…</div>

  const parents = lin.edges.filter((e) => e.child === uri)
  const children = lin.edges.filter((e) => e.parent === uri)
  const name = (u: string) => lin.nodes.find((n) => n.uri === u)?.name ?? u.split('/').slice(-1)[0]

  return (
    <div style={{ padding: 14, fontSize: 12.5 }}>
      <Section label="Parents" empty="no upstream datasets">
        {parents.map((e, i) => <Row key={i} name={name(e.parent)} sub={e.pipeline ?? undefined} up />)}
      </Section>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 2px' }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: color.focus }} />
        <span style={{ fontWeight: 600 }}>{name(uri!)}</span>
        <span style={{ fontSize: 10.5, color: color.text3 }}>this node</span>
      </div>
      <Section label="Children" empty="no downstream datasets yet">
        {children.map((e, i) => <Row key={i} name={name(e.child)} sub={e.pipeline ?? undefined} />)}
      </Section>
    </div>
  )
}

function Section({ label, empty, children }: { label: string; empty: string; children: React.ReactNode }) {
  const has = Array.isArray(children) ? children.length > 0 : !!children
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', color: color.text3, marginBottom: 4 }}>{label}</div>
      {has ? children : <div style={{ fontSize: 11, color: color.text3, paddingLeft: 2 }}>{empty}</div>}
    </div>
  )
}

function Row({ name, sub, up }: { name: string; sub?: string; up?: boolean }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 2px', color: color.text2 }}>
      <span style={{ color: color.text3 }}><Icon name={up ? 'chevronRight' : 'arrow'} size={12} /></span>
      <span style={{ fontWeight: 600, color: color.ink }}>{name}</span>
      {sub && <span style={{ fontSize: 10, color: color.text3 }}>· {sub}</span>}
    </div>
  )
}

function resolveUri(doc: ReturnType<typeof useStore.getState>['doc'], nodeId: string): string | undefined {
  const seen = new Set<string>()
  const walk = (id: string): string | undefined => {
    if (seen.has(id)) return undefined
    seen.add(id)
    const n = doc.nodes.find((x) => x.id === id)
    if (!n) return undefined
    if (n.data.config.uri) return n.data.config.uri as string
    for (const e of doc.edges) if (e.target === id) { const u = walk(e.source); if (u) return u }
    return undefined
  }
  return walk(nodeId)
}
