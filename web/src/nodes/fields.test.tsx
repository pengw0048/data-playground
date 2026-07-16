import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ColumnListPicker } from './fields'

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
