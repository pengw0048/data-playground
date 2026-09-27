import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ColumnCombo, ColumnListPicker, SortBuilder } from './fields'
import { useStore } from '../store/graph'

vi.mock('../api/client', () => ({ api: new Proxy({}, { get: () => async () => ({}) }) }))

describe('ColumnListPicker', () => {
  it('keeps selected fields as an ordered array', () => {
    const onChange = vi.fn()
    const columns = [
      { name: 'id', type: 'BIGINT', capabilities: [] },
      { name: 'event', type: 'VARCHAR', capabilities: [] },
      { name: 'amount', type: 'DOUBLE', capabilities: [] },
    ]
    const { rerender } = render(<ColumnListPicker value={['event']} columns={columns} onChange={onChange} />)

    fireEvent.click(screen.getByRole('button', { name: '+ add column' }))
    expect(onChange).toHaveBeenLastCalledWith(['event', 'id'])

    rerender(<ColumnListPicker value={['event', 'id']} columns={columns} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Move id up' }))
    expect(onChange).toHaveBeenLastCalledWith(['id', 'event'])
  })
})


describe('SQL column suggestions', () => {
  it('quotes suggested group keys without rewriting free-form expressions', () => {
    const onChange = vi.fn()
    render(<ColumnCombo quoteIdentifiers value="" columns={[
      { name: 'kind, "label"', type: 'VARCHAR', capabilities: [] },
    ]} onChange={onChange} />)
    expect(screen.getByRole('option', { hidden: true })).toHaveValue('"kind, ""label"""')
    fireEvent.change(screen.getByRole('combobox'), { target: { value: "date_trunc('month', created_at)" } })
    expect(onChange).toHaveBeenCalledWith("date_trunc('month', created_at)")
  })
})

describe('SortBuilder', () => {
  beforeEach(() => {
    useStore.setState({
      doc: { id: 'sort-fields', version: 1, nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'Source', status: 'draft', config: {} } },
        { id: 'sort', type: 'sort', position: { x: 300, y: 0 }, data: { title: 'Sort', status: 'draft', config: { by: '"kind, ""label""" DESC NULLS LAST' } } },
      ], edges: [{ id: 'edge', source: 'source', target: 'sort', sourceHandle: 'out', targetHandle: 'in', data: { wire: 'dataset' } }] },
      schemas: { source: { out: ['kind, "label"', 'ASC', 'DESC'].map((name) => ({ name, type: 'VARCHAR', capabilities: [] })) } },
      currentUser: null, canvasRole: 'owner', previews: {}, catalog: [], runs: {}, numericParamDrafts: {},
    })
  })
  const stored = () => useStore.getState().doc.nodes.find((node) => node.id === 'sort')!.data.config.by

  it('edits raw names and direction without losing explicit null ordering', () => {
    render(<SortBuilder nodeId="sort" />)
    expect(screen.getByPlaceholderText('column')).toHaveValue('kind, "label"')
    expect(screen.getAllByRole('option', { name: 'VARCHAR', hidden: true }).map((option) => option.getAttribute('value')))
      .toEqual(['kind, "label"', 'ASC', 'DESC'])
    expect(screen.getByLabelText('Null placement for sort key 1')).toHaveValue('LAST')
    fireEvent.click(screen.getByTitle('Toggle direction'))
    expect(stored()).toBe('"kind, ""label""" ASC NULLS LAST')
    fireEvent.change(screen.getByPlaceholderText('column'), { target: { value: 'DESC' } })
    expect(stored()).toBe('"DESC" ASC NULLS LAST')
    fireEvent.change(screen.getByLabelText('Null placement for sort key 1'), { target: { value: 'FIRST' } })
    expect(stored()).toBe('"DESC" ASC NULLS FIRST')
    fireEvent.click(screen.getByRole('button', { name: 'add sort key' }))
    expect(stored()).toBe('"DESC" ASC NULLS FIRST, "kind, ""label""" ASC')
  })

  it('keeps the same input while clearing and typing spaces, quotes, and commas', () => {
    render(<SortBuilder nodeId="sort" />)
    const input = screen.getByPlaceholderText('column')
    fireEvent.change(input, { target: { value: '' } })
    expect(screen.getByPlaceholderText('column')).toBe(input)
    expect(input).toHaveValue('')
    for (const value of ['sale ', 'sale "', 'sale "USD", DESC']) {
      fireEvent.change(input, { target: { value } })
      expect(input).toHaveValue(value)
      expect(screen.queryByPlaceholderText('score DESC, id')).not.toBeInTheDocument()
    }
    expect(stored()).toBe('"sale ""USD"", DESC" DESC NULLS LAST')
  })

  it.each(['score + penalty DESC', 'coalesce(score, 0) DESC NULLS FIRST'])('leaves an expression unchanged in raw mode: %s', (by) => {
    useStore.getState().updateConfig('sort', { by })
    render(<SortBuilder nodeId="sort" />)
    expect(screen.getByPlaceholderText('score DESC, id')).toHaveValue(by)
    expect(screen.getByRole('button', { name: 'builder' })).toBeDisabled()
    expect(stored()).toBe(by)
  })

  it('shows external changes instead of retaining a stale local column draft', () => {
    render(<SortBuilder nodeId="sort" />)
    fireEvent.change(screen.getByPlaceholderText('column'), { target: { value: '' } })
    act(() => useStore.getState().updateConfig('sort', { by: '"ASC" DESC NULLS FIRST' }))
    expect(screen.getByPlaceholderText('column')).toHaveValue('ASC')
    expect(screen.getByLabelText('Null placement for sort key 1')).toHaveValue('FIRST')
  })
})
