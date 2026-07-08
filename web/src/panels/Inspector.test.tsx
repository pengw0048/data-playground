import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { PortRow, canDeclareSchemaKind } from './Inspector'
import type { ColumnSchema } from '../types/graph'

const cols: ColumnSchema[] = [
  { name: 'id', type: 'int', capabilities: [] },
  { name: 'amount', type: 'double', capabilities: [] },
]

describe('canDeclareSchemaKind — which kinds can carry a schema contract', () => {
  it('is true for code ops + any plugin kind', () => {
    for (const k of ['transform', 'notebook', 'vector-search', 'loop', 'opaque', 'my_plugin_node']) {
      expect(canDeclareSchemaKind(k)).toBe(true)
    }
  })
  it('is false for relational / io / annotation built-ins (never a phantom contract editor)', () => {
    for (const k of ['source', 'filter', 'select', 'sort', 'dedup', 'join', 'sql', 'aggregate',
      'sample', 'metric', 'chart', 'write', 'note', 'section', 'code']) {
      expect(canDeclareSchemaKind(k)).toBe(false)
    }
  })
})

describe('PortRow — port schema badge', () => {
  it('typed → "N cols" badge that expands to each column name:type', () => {
    render(<PortRow dir="out" name={null} wire="dataset" schema={cols} />)
    const badge = screen.getByText('2 cols')
    expect(badge).toBeInTheDocument()
    fireEvent.click(badge)                                   // expandable → show the columns
    expect(screen.getByText('id')).toBeInTheDocument()
    expect(screen.getByText('amount')).toBeInTheDocument()
    expect(screen.getByText('double')).toBeInTheDocument()
  })
  it('untyped (null) → amber "untyped" badge, not expandable', () => {
    render(<PortRow dir="in" name={null} wire="dataset" schema={null} />)
    expect(screen.getByText('untyped')).toBeInTheDocument()
    expect(screen.queryByText('id')).not.toBeInTheDocument()
  })
  it('unknown (undefined) → no badge at all', () => {
    render(<PortRow dir="in" name={null} wire="dataset" schema={undefined} />)
    expect(screen.queryByText(/cols$/)).not.toBeInTheDocument()
    expect(screen.queryByText('untyped')).not.toBeInTheDocument()
  })
})
