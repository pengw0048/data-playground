import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { LineageResult } from '../types/api'
import { Icon } from '../ui/Icon'

const message = (error: unknown) => error instanceof Error ? error.message : String(error)

export function DatasetLineageSummary({ uri, name, onOpenDataset }: {
  uri: string | null | undefined
  name: string
  onOpenDataset?: (catalogId: string) => void
}) {
  const [lineage, setLineage] = useState<LineageResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const request = useRef(0)

  const load = useCallback(async () => {
    if (!uri) return
    const sequence = ++request.current
    setLoading(true)
    setError(null)
    try {
      const next = await api.lineage(uri, 4, 60)
      if (sequence === request.current) setLineage(next)
    } catch (caught) {
      if (sequence === request.current) setError(message(caught))
    } finally {
      if (sequence === request.current) setLoading(false)
    }
  }, [uri])

  useEffect(() => {
    request.current += 1
    setLineage(null)
    setError(null)
    setLoading(false)
    void load()
    return () => { request.current += 1 }
  }, [load])

  const root = lineage?.rootUri
  const lineageNode = (nodeUri: string) => lineage?.nodes.find((node) => node.uri === nodeUri)
  const nodeName = (nodeUri: string) => {
    const path = nodeUri.split('/').filter(Boolean)
    return lineageNode(nodeUri)?.name ?? path[path.length - 1] ?? nodeUri
  }
  const parents = root ? lineage?.edges.filter((edge) => edge.child === root) ?? [] : []
  const children = root ? lineage?.edges.filter((edge) => edge.parent === root) ?? [] : []
  const linked = (label: string, nodeUri: string) => {
    const node = lineageNode(nodeUri)
    // The lineage service falls back to the stable URI as an id when the endpoint has no Catalog
    // registration. Such evidence is useful to display, but it is not a navigable dataset route.
    const catalogId = node?.id && node.id !== node.uri ? node.id : null
    return onOpenDataset && catalogId
    ? <button type="button" onClick={() => onOpenDataset(catalogId)}
      className="max-w-full truncate rounded-md border border-border bg-background px-2 py-1 text-left text-[10.5px] font-semibold text-primary hover:bg-accent"
      title={`Open ${label}`}>{label}</button>
    : <span className="max-w-full truncate rounded-md border border-border bg-background px-2 py-1 text-[10.5px] font-semibold text-foreground" title={label}>{label}</span>
  }

  return <section data-testid="dataset-lineage-summary" className="rounded-lg border border-border bg-card px-3 py-2.5">
    <div className="flex items-center gap-2">
      <Icon name="lineage" size={13} />
      <h2 className="text-[11px] font-semibold text-foreground">Lineage</h2>
      {lineage?.truncated && <span className="text-[10px] text-muted-foreground">Showing nearby datasets</span>}
    </div>
    {!uri ? <div className="mt-1 text-[11px] text-muted-foreground">Lineage becomes available after this dataset has a stable Source binding.</div> : null}
    {loading && !lineage ? <div role="status" className="mt-1 text-[11px] text-muted-foreground">Loading lineage…</div> : null}
    {error ? <div role="alert" className="mt-1 flex items-center justify-between gap-2 text-[11px] text-destructive">
      <span>Couldn't load lineage: {error}</span>
      <button type="button" onClick={() => void load()} className="shrink-0 font-semibold underline">Retry</button>
    </div> : null}
    {lineage && parents.length === 0 && children.length === 0 ? <div className="mt-1 text-[11px] text-muted-foreground">No recorded inputs or outputs yet.</div> : null}
    {lineage && (parents.length > 0 || children.length > 0) ? <div className="mt-2 grid min-w-0 items-center gap-2 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto_minmax(0,1fr)]">
      <div className="grid min-w-0 gap-1" aria-label="Input datasets">
        <span className="text-[9.5px] font-semibold uppercase tracking-wide text-muted-foreground">Inputs</span>
        {parents.length ? parents.map((edge) => <span key={`${edge.parent}:${edge.child}`}>{linked(nodeName(edge.parent), edge.parent)}</span>)
          : <span className="text-[10.5px] text-muted-foreground">None recorded</span>}
      </div>
      <span aria-hidden="true" className="hidden text-muted-foreground sm:block">→</span>
      <div className="min-w-0 rounded-md bg-primary/10 px-2 py-1 text-center text-[10.5px] font-semibold text-primary" title={name}>{name}</div>
      <span aria-hidden="true" className="hidden text-muted-foreground sm:block">→</span>
      <div className="grid min-w-0 gap-1" aria-label="Output datasets">
        <span className="text-[9.5px] font-semibold uppercase tracking-wide text-muted-foreground">Outputs</span>
        {children.length ? children.map((edge) => <span key={`${edge.parent}:${edge.child}`}>{linked(nodeName(edge.child), edge.child)}</span>)
          : <span className="text-[10.5px] text-muted-foreground">None recorded</span>}
      </div>
    </div> : null}
  </section>
}
