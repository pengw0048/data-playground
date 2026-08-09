import { useEffect, useState } from 'react'
import { api, type ExecutionManifestDocument } from '../api/client'
import { normalizeTimeBucket, type TimeBucket } from './chartTemporal'

export type ArtifactPresentation =
  | {
    kind: 'chart'
    type: string
    xLabel: string
    yLabel: string
    grouped: boolean
    seriesLabel?: string
    timeBucket?: TimeBucket
  }
  | { kind: 'metric' }

const record = (value: unknown): Record<string, unknown> | null => (
  value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
)

export function artifactPresentationFromManifest(
  document: ExecutionManifestDocument,
  nodeId: string,
  chartType = 'bar',
): ArtifactPresentation | undefined {
  const node = document.graph.nodes.map(record).find((candidate) => candidate?.id === nodeId)
  if (!node) return undefined
  if (node.type === 'metric') return { kind: 'metric' }
  if (node.type !== 'chart') return undefined
  const data = record(node.data)
  const config = record(data?.config) ?? {}
  const agg = String(config.agg ?? 'count')
  return {
    kind: 'chart',
    type: chartType,
    xLabel: String(config.x || 'All rows'),
    grouped: agg !== 'none',
    seriesLabel: String(config.series || '') || undefined,
    timeBucket: normalizeTimeBucket(config.timeBucket),
    yLabel: agg !== 'none' ? `${agg}(${String(config.y ?? '*')})` : String(config.y ?? 'y'),
  }
}

/** Recover immutable semantic presentation from a run manifest and the current visual chart type. */
export function useRunArtifactPresentation(
  canvasId: string | null | undefined,
  subjectId: string | null | undefined,
  nodeId: string | null | undefined,
  enabled = true,
): ArtifactPresentation | undefined {
  const [presentation, setPresentation] = useState<ArtifactPresentation>()

  useEffect(() => {
    let live = true
    setPresentation(undefined)
    if (!enabled || !canvasId || !subjectId || !nodeId) return () => { live = false }

    void (async () => {
      try {
        const detail = await api.executionManifest(canvasId, subjectId)
        if (!live || detail.availability !== 'available' || !detail.document) return
        let chartType = 'bar'
        const manifestPresentation = artifactPresentationFromManifest(detail.document, nodeId)
        if (manifestPresentation?.kind === 'chart') {
          try {
            const current = await api.getCanvas(canvasId)
            const currentNode = current.nodes.find((node) => node.id === nodeId && node.type === 'chart')
            const candidate = String(currentNode?.data.config.chartType ?? '')
            if (['bar', 'line', 'scatter', 'area'].includes(candidate)) chartType = candidate
          } catch {
            // The immutable manifest still gives a truthful result shape when the current Canvas
            // is unavailable; only the presentation-only chart type falls back to Bars.
          }
        }
        if (live) setPresentation(artifactPresentationFromManifest(detail.document, nodeId, chartType))
      } catch {
        // Older/pruned manifests remain readable as tables. Never infer semantic Chart fields from
        // a newer Canvas version merely to force a visualization.
      }
    })()

    return () => { live = false }
  }, [canvasId, enabled, nodeId, subjectId])

  return presentation
}
