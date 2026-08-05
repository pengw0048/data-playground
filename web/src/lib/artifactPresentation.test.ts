import { describe, expect, it } from 'vitest'
import type { ExecutionManifestDocument } from '../api/client'
import { artifactPresentationFromManifest } from './artifactPresentation'

const document: ExecutionManifestDocument = {
  schemaVersion: 1,
  graph: {
    requirements: [], edges: [], nodes: [{
      id: 'chart', type: 'chart', data: { config: {
        agg: 'sum', x: 'split', y: 'amount', series: 'model',
      } },
    }],
  },
  target: { nodeId: 'chart', portId: 'out' },
  admittedInputs: [],
  descriptors: {},
}

describe('artifact presentation recovery', () => {
  it('uses immutable semantic Chart fields with the current presentation-only type', () => {
    expect(artifactPresentationFromManifest(document, 'chart', 'line')).toEqual({
      kind: 'chart', type: 'line', xLabel: 'split', yLabel: 'sum(amount)', grouped: true,
      seriesLabel: 'model',
    })
  })

  it('does not guess presentation for an unrelated output', () => {
    expect(artifactPresentationFromManifest(document, 'missing')).toBeUndefined()
  })
})
