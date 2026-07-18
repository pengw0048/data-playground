import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { NodeFinder, findNodeSpecs, portSummary } from './NodeFinder'
import type { NodeSpec } from '../nodes/registry'

const node = (overrides: Partial<NodeSpec>): NodeSpec => ({
  kind: 'filter', title: 'filter', category: 'shape', inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset'] }], outputs: [{ id: 'out', wire: 'dataset' }],
  canBypass: true, blurb: 'row predicate', defaultData: () => ({ title: 'filter', status: 'draft', config: {} }), ...overrides,
})

describe('node finder', () => {
  it('searches registry metadata, prefers compatible exact results, and stays deterministic', () => {
    const specs = [
      node({ kind: 'same-plugin', title: 'Filter', source: 'plugin:quality-pack', inputs: [{ id: 'in', wire: 'metric', accepts: ['metric'] }] }),
      node({ kind: 'filter', title: 'Filter', source: 'builtin' }),
      node({ kind: 'profile', title: 'Profile', category: 'inspect', blurb: 'filter diagnostics' }),
    ]
    expect(findNodeSpecs(specs, 'filter', 'dataset').map((result) => result.spec.kind)).toEqual(['filter', 'profile', 'same-plugin'])
    expect(findNodeSpecs(specs, 'metric').map((result) => result.spec.kind)).toEqual(['same-plugin'])
    expect(portSummary(specs[0])).toBe('in metric · out dataset')
  })

  it('adds the highlighted result with Enter and closes with Escape', () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    render(<NodeFinder specs={[node({ source: 'plugin:quality-pack' })]} onPick={onPick} onClose={onClose} />)
    const search = screen.getByRole('textbox', { name: 'Search nodes' })
    expect(search).toHaveFocus()
    expect(screen.getByRole('option').textContent).toContain('Plugin · quality-pack')
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(onPick).toHaveBeenCalledWith('filter')
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
