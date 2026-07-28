import { describe, it, expect } from 'vitest'
import { examples, exampleDoc } from './examples'

describe('example canvases — runnable starters', () => {
  it('exposes at least three examples with a name + blurb', () => {
    expect(examples.length).toBeGreaterThanOrEqual(3)
    for (const e of examples) expect(e.key && e.name && e.blurb).toBeTruthy()
  })

  it('describes each example by its data outcome in one concise sentence', () => {
    expect(Object.fromEntries(examples.map((example) => [example.key, example.blurb]))).toEqual({
      purchases: 'Rank users by total purchase amount and save the result.',
      top3: 'Keep each user’s three highest-value events.',
      quality: 'Flag events with an amount above 100.',
    })
    expect(examples.map((example) => example.blurb).join(' ')).not.toMatch(
      /\bbasics\b|shows the .* node|preview the node/i,
    )
  })

  it('each example builds a structurally valid, connected doc on the given id', () => {
    for (const e of examples) {
      const doc = exampleDoc(e.key, 'canvas_test')!
      expect(doc.id).toBe('canvas_test')
      expect(doc.nodes.length).toBeGreaterThan(0)
      const ids = new Set(doc.nodes.map((n) => n.id))
      // every edge connects two real nodes; every node carries a config
      for (const ed of doc.edges) {
        expect(ids.has(ed.source) && ids.has(ed.target)).toBe(true)
      }
      for (const n of doc.nodes) expect(n.data.config).toBeTruthy()
      // a linear starter: edges = nodes - 1, and every node is reachable from the first
      expect(doc.edges.length).toBe(doc.nodes.length - 1)
      expect(doc.nodes[0].type).toBe('source')
    }
  })

  it('sources reference the seeded datasets by bare name (portable, resolve via the catalog)', () => {
    const doc = exampleDoc('purchases', 'c')!
    const src = doc.nodes.find((n) => n.type === 'source')!
    expect(src.data.config.uri).toBe('events')
  })

  it('returns null for an unknown key', () => {
    expect(exampleDoc('nope', 'c')).toBeNull()
  })
})
