import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { NodeFinder, findNodeSpecs, portFinderPlacement } from './NodeFinder'
import type { NodeSpec } from '../nodes/registry'

const node = (overrides: Partial<NodeSpec>): NodeSpec => ({
  kind: 'filter', title: 'filter', category: 'shape', inputs: [{ id: 'in', wire: 'dataset', accepts: ['dataset'] }], outputs: [{ id: 'out', wire: 'dataset' }],
  canBypass: true, blurb: 'row predicate', defaultData: () => ({ title: 'filter', status: 'draft', config: {} }), ...overrides,
})

describe('node finder', () => {
  it('keeps an anchored picker beside its port and inside the Canvas boundary', () => {
    expect(portFinderPlacement(
      { left: 600, right: 615, top: 200, bottom: 215 },
      { left: 0, right: 724, top: 0, bottom: 650 },
      720,
    )).toEqual({ left: 192, top: 200, width: 400, maxHeight: 442 })

    expect(portFinderPlacement(
      { left: 100, right: 115, top: 580, bottom: 595 },
      { left: 0, right: 724, top: 0, bottom: 650 },
      720,
    )).toEqual({ left: 123, bottom: 125, width: 400, maxHeight: 480 })
  })

  it('keeps exact name matches ahead of compatibility and hides weaker metadata matches', () => {
    const specs = [
      node({ kind: 'same-plugin', title: 'Filter', source: 'plugin:quality-pack', inputs: [{ id: 'in', wire: 'metric', accepts: ['metric'] }] }),
      node({ kind: 'filter', title: 'Filter', source: 'builtin' }),
      node({ kind: 'profile', title: 'Profile', category: 'inspect', blurb: 'filter diagnostics' }),
    ]
    expect(findNodeSpecs(specs, 'filter', 'dataset').map((result) => result.spec.kind)).toEqual(['filter', 'same-plugin'])
    expect(findNodeSpecs(specs, 'metric').map((result) => result.spec.kind)).toEqual(['same-plugin'])
  })

  it('narrows operation-name prefixes while keeping intentional category and port searches discoverable', () => {
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

  it('prefers exact category matches over incidental internal-name substrings', () => {
    const specs = [
      node({ kind: 'source', title: 'Source', category: 'io', inputs: [], outputs: [{ id: 'out', wire: 'dataset' }] }),
      node({ kind: 'write', title: 'Write', category: 'io' }),
      node({ kind: 'union', title: 'Union', category: 'compute' }),
      node({ kind: 'section', title: 'Section', category: 'compute' }),
    ]

    expect(findNodeSpecs(specs, 'io').map((result) => result.spec.kind)).toEqual(['source', 'write'])
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

  it('uses the same keyboard-first search in a non-modal port popover', () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    const specs = [
      node({ kind: 'transform', title: 'Transform', source: 'plugin:quality-pack' }),
      node({ kind: 'metric-only', title: 'Metric only', inputs: [{ id: 'metric', wire: 'metric', accepts: ['metric'] }] }),
    ]
    render(<NodeFinder
      specs={specs}
      wire="dataset"
      compatibleOnly
      anchor={{ left: 200, right: 215, top: 150, bottom: 165 }}
      boundary={{ left: 0, right: 900, top: 0, bottom: 700 }}
      onPick={onPick}
      onClose={onClose}
    />)

    const search = screen.getByRole('textbox', { name: 'Search operations' })
    const dialog = screen.getByRole('dialog', { name: 'Connect to an operation' })
    expect(dialog).toBeVisible()
    expect(dialog).not.toHaveAttribute('aria-modal')
    expect(dialog.closest('.dp-modal-overlay')).toBeNull()
    expect(dialog).toHaveStyle({ left: '223px', width: '400px' })
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
