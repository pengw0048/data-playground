import type { APIRequestContext } from '@playwright/test'
import type { CanvasDoc } from '../../src/types/graph'

export function goldenCanvas(id: string, name: string, sourceTitle: string): CanvasDoc {
  return {
    id,
    name,
    version: 1,
    nodes: [
      {
        id: 'source', type: 'source', position: { x: 80, y: 180 },
        data: {
          title: sourceTitle, status: 'latest', config: { uri: 'events' },
          lastRun: { rows: 2_000, ms: 10, placement: 'local' },
        },
      },
      {
        id: 'filter', type: 'filter', position: { x: 390, y: 180 },
        data: {
          title: 'UX golden filter', status: 'latest',
          // OR forces the raw-SQL control, making the invalidation edit deterministic in the browser.
          config: { predicate: "event = 'purchase' OR amount > 0" },
          lastRun: { rows: 500, ms: 12, placement: 'local' },
        },
      },
    ],
    edges: [{ id: 'source-filter', source: 'source', target: 'filter', data: { wire: 'dataset' } }],
  }
}

export async function installCanvas(request: APIRequestContext, doc: CanvasDoc) {
  const response = await request.post('/api/canvas', { data: doc })
  if (!response.ok()) throw new Error(`could not create fixture canvas ${doc.id}: ${response.status()}`)
}
