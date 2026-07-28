import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { NodeFinder, findNodeSpecs } from './NodeFinder'
import type { NodeSpec } from '../nodes/registry'

const node = (overrides: Partial<NodeSpec>): NodeSpec => ({
  kind: 'filter', title: 'filter', category: 'shape', inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset'] }], outputs: [{ id: 'out', wire: 'dataset' }],
  canBypass: true, blurb: 'row predicate', defaultData: () => ({ title: 'filter', status: 'draft', config: {} }), ...overrides,
})

describe('node finder', () => {
  it('keeps exact name matches ahead of compatibility and hides weaker metadata matches', () => {
    const specs = [
      node({ kind: 'same-plugin', title: 'Filter', source: 'plugin:quality-pack', inputs: [{ id: 'in', wire: 'metric', accepts: ['metric'] }] }),
      node({ kind: 'filter', title: 'Filter', source: 'builtin' }),
      node({ kind: 'profile', title: 'Profile', category: 'inspect', blurb: 'filter diagnostics' }),
    ]
    expect(findNodeSpecs(specs, 'filter', 'dataset').map((result) => result.spec.kind)).toEqual(['filter', 'same-plugin'])
    expect(findNodeSpecs(specs, 'metric').map((result) => result.spec.kind)).toEqual(['same-plugin'])
  })

  it('narrows prefixes to name matches and falls back to purpose metadata', () => {
    const specs = [
      node({ kind: 'sample', title: 'Sample', blurb: 'choose representative rows' }),
      node({ kind: 'sample-balanced', title: 'Balanced sample', blurb: 'choose by label' }),
      node({ kind: 'profile', title: 'Profile', blurb: 'sample diagnostics' }),
      node({ kind: 'inspect', title: 'Inspect', blurb: 'choose representative rows' }),
    ]

    expect(findNodeSpecs(specs, 'sam').map((result) => result.spec.kind)).toEqual(['sample', 'sample-balanced'])
    expect(findNodeSpecs(specs, 'representative').map((result) => result.spec.kind)).toEqual(['inspect', 'sample'])
    expect(findNodeSpecs(specs, 'not-present')).toEqual([])
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

  it('keeps a single operation title and hides registry metadata for an unambiguous result', () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    render(<NodeFinder specs={[node({ source: 'plugin:quality-pack' })]} onPick={onPick} onClose={onClose} />)
    const search = screen.getByRole('textbox', { name: 'Search operations' })
    expect(search).toHaveFocus()
    const option = screen.getByRole('option')
    expect(option).toHaveTextContent('filter')
    expect(option).toHaveTextContent('row predicate')
    expect(option).not.toHaveTextContent('Built-in')
    expect(option).not.toHaveTextContent('Plugin · quality-pack')
    expect(option).not.toHaveTextContent('in dataset')
    expect(option).not.toHaveTextContent('out dataset')
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(onPick).toHaveBeenCalledWith('filter')
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('adds the top exact match with Enter even when a compatible prefix match exists', () => {
    const onPick = vi.fn()
    render(<NodeFinder specs={[
      node({ kind: 'sample', title: 'Sample', inputs: [{ id: 'in', wire: 'metric', accepts: ['metric'] }] }),
      node({ kind: 'sample-rows', title: 'Sample rows' }),
    ]} wire="dataset" onPick={onPick} onClose={vi.fn()} />)

    const search = screen.getByRole('textbox', { name: 'Search operations' })
    fireEvent.change(search, { target: { value: 'sample' } })
    expect(screen.getAllByRole('option')).toHaveLength(2)
    expect(screen.getAllByRole('option')[0]).toHaveTextContent('Sample')
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(onPick).toHaveBeenCalledWith('sample')
  })

  it('shows a concise cue only when duplicate human titles need distinction', () => {
    render(<NodeFinder specs={[
      node({ kind: 'filter', title: 'Filter', category: 'shape' }),
      node({ kind: 'quality-filter', title: 'Filter', category: 'inspect', source: 'plugin:quality-pack' }),
    ]} onPick={vi.fn()} onClose={vi.fn()} />)

    const options = screen.getAllByRole('option')
    expect(options[0]).toHaveTextContent('shape')
    expect(options[0]).not.toHaveTextContent('filter filter')
    expect(options[1]).toHaveTextContent('Plugin · quality-pack')
    expect(options[1]).not.toHaveTextContent('quality-filter')
  })

  it('uses the same keyboard-first picker for compatible port connections', () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    const specs = [
      node({ kind: 'transform', title: 'Transform', source: 'plugin:quality-pack' }),
      node({ kind: 'metric-only', title: 'Metric only', inputs: [{ id: 'metric', wire: 'metric', accepts: ['metric'] }] }),
    ]
    render(<NodeFinder specs={specs} wire="dataset" compatibleOnly onPick={onPick} onClose={onClose} />)

    const search = screen.getByRole('textbox', { name: 'Search operations' })
    expect(screen.getByRole('dialog', { name: 'Connect to an operation' })).toBeVisible()
    expect(search).toHaveFocus()
    expect(screen.getAllByRole('option')).toHaveLength(1)
    expect(screen.getByText('For dataset output')).toBeVisible()
    expect(screen.getByRole('option')).not.toHaveTextContent('compatible')
    expect(screen.getByRole('option')).not.toHaveTextContent('in dataset')
    fireEvent.change(search, { target: { value: 't' } })
    fireEvent.keyDown(search, { key: 'ArrowDown' })
    fireEvent.keyDown(search, { key: 'ArrowUp' })
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(onPick).toHaveBeenCalledWith('transform')
    fireEvent.click(screen.getByRole('option'))
    expect(onPick).toHaveBeenCalledTimes(2)
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('renders only the first 100 results while reporting a truncated full search', () => {
    const specs = Array.from({ length: 101 }, (_, index) => node({ kind: `plugin-${index}`, title: `Plugin ${index}` }))
    render(<NodeFinder specs={specs} onPick={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getAllByRole('option')).toHaveLength(100)
    expect(screen.getByText('Showing first 100 of 101')).toBeVisible()
  })
})
