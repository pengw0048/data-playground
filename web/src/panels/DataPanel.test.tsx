import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ColumnSchema } from '../types/graph'
import { RowDetail, RowsTable } from './DataPanel'

const col = (name: string, type: string): ColumnSchema => ({
  name, type, capabilities: [], provenance: 'inferred',
})

describe('Rows grid', () => {
  it('renders integers beyond 2^53 as the exact digits the kernel sent', () => {
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

const whitespaceColumns: ColumnSchema[] = [{ name: 'val', type: 'string', capabilities: [] }]
const whitespaceValues = ['trail  ', '  lead', '  ', '\t', 'a\nb', 'plain text', '', null]
const whitespaceRows = whitespaceValues.map((val) => ({ val }))

describe('whitespace in string cells', () => {
  it('shows hidden whitespace in the grid and keeps NULL, empty and clean values apart', () => {
    render(<RowsTable columns={whitespaceColumns} rows={whitespaceRows} onRowClick={() => {}} />)
    expect(screen.getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      'trail␣␣', '␣␣lead', '␣␣', '⇥', 'a↵\nb', 'plain text', '', '·',
    ])
  })

  it('names the whitespace it marks', () => {
    render(<RowsTable columns={whitespaceColumns} rows={whitespaceRows} onRowClick={() => {}} />)
    expect(screen.getAllByTitle('2 spaces')).toHaveLength(3)
    expect(screen.getByTitle('1 tab')).toBeTruthy()
    expect(screen.getByTitle('1 newline')).toBeTruthy()
  })

  it('shows hidden whitespace in the row detail', () => {
    const { container } = render(<RowDetail columns={whitespaceColumns} row={{ val: 'trail  ' }} />)
    expect(container.textContent).toContain('trail␣␣')
  })
})
