import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ExistingNodeLocator, findExistingNodes } from './ExistingNodeLocator'
import type { CanvasNode } from '../types/graph'

const node = (overrides: Partial<CanvasNode>): CanvasNode => ({
  id: 'node-1', type: 'filter', position: { x: 0, y: 0 },
  data: { title: 'Orders', status: 'draft', config: {}, meta: 'row predicate' }, ...overrides,
})

describe('existing node locator', () => {
  it('searches the current document by title, kind, stable ID, status, and output labels in deterministic order', () => {
    const nodes = [
      node({ id: 'filter-2', type: 'filter', data: { title: 'Duplicate', status: 'failed', config: {}, disabled: true } }),
      node({ id: 'filter-1', type: 'filter', data: { title: 'Duplicate', status: 'stale', config: {} } }),
      node({ id: 'metric-stable-id', type: 'metric', data: { title: 'Count', status: 'latest', config: {} } }),
      node({ id: 'section-1', type: 'section', data: { title: 'Driver', status: 'draft', config: { outputs: ['published'] } } }),
    ]

    expect(findExistingNodes(nodes, 'duplicate').results.map((result) => result.node.id)).toEqual(['filter-1', 'filter-2'])
    expect(findExistingNodes(nodes, 'metric-stable-id').results.map((result) => result.node.id)).toEqual(['metric-stable-id'])
    expect(findExistingNodes(nodes, 'failed').results.map((result) => result.node.id)).toEqual(['filter-2'])
    expect(findExistingNodes(nodes, 'published').results.map((result) => result.node.id)).toEqual(['section-1'])
  })

  it('chooses an existing node without exposing an add operation path', () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    render(<ExistingNodeLocator nodes={[node({ id: 'off-screen-node', data: { title: 'Off screen', status: 'stale', config: {} } })]} onPick={onPick} onClose={onClose} />)
    const search = screen.getByRole('textbox', { name: 'Search existing nodes' })
    expect(search).toHaveFocus()
    fireEvent.change(search, { target: { value: 'does-not-exist' } })
    expect(screen.getByText('No matching existing node.')).toBeVisible()
    fireEvent.change(search, { target: { value: '' } })
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(onPick).toHaveBeenCalledWith('off-screen-node')
    expect(screen.getByRole('option')).toHaveTextContent('filter · off-screen-node')
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('retains and renders only the best 100 results while still reporting the full match count', () => {
    const nodes = [
      ...Array.from({ length: 100 }, (_, index) => node({ id: `node-${index}`, data: { title: `Target ${index}`, status: 'draft', config: {} } })),
      node({ id: 'exact-last', data: { title: 'Target', status: 'draft', config: {} } }),
    ]
    const search = findExistingNodes(nodes, 'target')
    expect(search.total).toBe(101)
    expect(search.results).toHaveLength(100)
    expect(search.results[0].node.id).toBe('exact-last')
    render(<ExistingNodeLocator nodes={nodes} onPick={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getAllByRole('option')).toHaveLength(100)
    expect(screen.getByText('Showing first 100 of 101')).toBeVisible()
  })
})
