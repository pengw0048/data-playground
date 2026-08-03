import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ColumnSchema } from '../types/graph'
import { RowDetail, RowsTable } from './DataPanel'

const columns: ColumnSchema[] = [{ name: 'val', type: 'string', capabilities: [] }]
const values = ['trail  ', '  lead', '  ', '\t', 'a\nb', 'plain text', '', null]
const rows = values.map((val) => ({ val }))

describe('whitespace in string cells', () => {
  it('shows hidden whitespace in the grid and keeps NULL, empty and clean values apart', () => {
    render(<RowsTable columns={columns} rows={rows} onRowClick={() => {}} />)
    expect(screen.getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      'trail␣␣', '␣␣lead', '␣␣', '⇥', 'a↵\nb', 'plain text', '', '·',
    ])
  })

  it('names the whitespace it marks', () => {
    render(<RowsTable columns={columns} rows={rows} onRowClick={() => {}} />)
    expect(screen.getAllByTitle('2 spaces')).toHaveLength(3)
    expect(screen.getByTitle('1 tab')).toBeTruthy()
    expect(screen.getByTitle('1 newline')).toBeTruthy()
  })

  it('shows hidden whitespace in the row detail', () => {
    const { container } = render(<RowDetail columns={columns} row={{ val: 'trail  ' }} />)
    expect(container.textContent).toContain('trail␣␣')
  })
})
