// Schema propagation for the editor (typed vs untyped ports). The kernel returns per-node OUTPUT
// columns where it can resolve them cheaply (relational ops); a code op / section is `null` here
// (untyped) until it has actually run, at which point we fall back to its last observed preview.
import type { CanvasDoc, ColumnSchema } from '../types/graph'
import type { CatalogTable, SampleResult } from '../types/api'

export type SchemaMap = Record<string, ColumnSchema[] | null>
export type PreviewMap = Record<string, { result?: SampleResult; loading?: boolean; error?: string }>

/** OUTPUT columns of a node: server schema (typed) → last observed preview → source catalog. */
export function nodeColumns(
  doc: CanvasDoc, schemas: SchemaMap, previews: PreviewMap, catalog: CatalogTable[], id: string,
): ColumnSchema[] {
  const s = schemas[id]
  if (s && s.length) return s
  const pv = previews[id]?.result?.columns
  if (pv && pv.length) return pv as ColumnSchema[]
  const n = doc.nodes.find((x) => x.id === id)
  if (n?.type === 'source' && n.data.config.uri) {
    const t = catalog.find((c) => c.uri === n.data.config.uri)
    if (t?.columns?.length) return t.columns as ColumnSchema[]
  }
  return []
}

/** INPUT columns available to a node = the OUTPUT columns of its upstream(s), de-duped by name. */
export function inputColumns(
  doc: CanvasDoc, schemas: SchemaMap, previews: PreviewMap, catalog: CatalogTable[], id: string,
): ColumnSchema[] {
  const sources = doc.edges.filter((e) => e.target === id).map((e) => e.source)
  const out: ColumnSchema[] = []
  const seen = new Set<string>()
  for (const src of sources) {
    for (const c of nodeColumns(doc, schemas, previews, catalog, src)) {
      if (!seen.has(c.name)) { seen.add(c.name); out.push(c) }
    }
  }
  return out
}
