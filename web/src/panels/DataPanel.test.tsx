import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RowsTable } from './DataPanel'
import type { ColumnSchema } from '../types/graph'

const col = (name: string, type: string): ColumnSchema => ({
  name, type, capabilities: [], provenance: 'inferred',
})

describe('Rows grid', () => {
  it('renders integers beyond 2^53 as the exact digits the kernel sent', () => {
    // the kernel ships out-of-safe-range integers as exact strings; the grid prints them verbatim
    render(<RowsTable
      columns={[col('id', 'int'), col('u', 'int'), col('n', 'int')]}
      rows={[
        { id: '9223372036854775807', u: '18446744073709551615', n: 42 },
        { id: '9223372036854775806', u: '0', n: -7 },
        { id: '-9223372036854775808', u: '18446744073709551614', n: null },
      ]}
      onRowClick={() => {}} />)

    for (const digits of ['9223372036854775807', '9223372036854775806', '-9223372036854775808',
      '18446744073709551615', '18446744073709551614']) {
      expect(screen.getByText(digits)).toBeInTheDocument()
    }
    expect(screen.queryByText('9223372036854776000')).not.toBeInTheDocument()
    expect(screen.getByText('42')).toBeInTheDocument()
  })

  it('right-aligns an integer column whose out-of-range cells arrive as strings', () => {
    render(<RowsTable columns={[col('id', 'int')]} rows={[{ id: '9223372036854775807' }]}
      onRowClick={() => {}} />)

    const cell = screen.getByText('9223372036854775807').closest('td')
    expect(cell).toHaveClass('text-right', 'tabular-nums')
  })
})
