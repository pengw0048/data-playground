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

  it('uses case-normalized code-point ordering for title and kind ties', () => {
    const specs = [
      node({ kind: 'beta', title: 'Same' }),
      node({ kind: 'alpha', title: 'same' }),
      node({ kind: 'umlaut', title: 'Älpha' }),
      node({ kind: 'zeta', title: 'Zulu' }),
    ]
    expect(findNodeSpecs(specs, '').map((result) => result.spec.kind)).toEqual(['alpha', 'beta', 'zeta', 'umlaut'])
  })

  it('adds the highlighted result with Enter and closes with Escape', () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    render(<NodeFinder specs={[node({ source: 'plugin:quality-pack' })]} onPick={onPick} onClose={onClose} />)
    const search = screen.getByRole('textbox', { name: 'Search operations' })
    expect(search).toHaveFocus()
    expect(screen.getByRole('option').textContent).toContain('Plugin · quality-pack')
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(onPick).toHaveBeenCalledWith('filter')
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('renders only the first 100 results while reporting a truncated full search', () => {
    const specs = Array.from({ length: 101 }, (_, index) => node({ kind: `plugin-${index}`, title: `Plugin ${index}` }))
    render(<NodeFinder specs={specs} onPick={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getAllByRole('option')).toHaveLength(100)
    expect(screen.getByText('Showing first 100 of 101')).toBeVisible()
  })
})
