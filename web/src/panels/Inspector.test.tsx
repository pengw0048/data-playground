import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { Inspector, PortRow, canDeclareNodeSchema, canDeclareSchemaKind } from './Inspector'
import type { ColumnSchema } from '../types/graph'
import { register } from '../nodes/registry'
import { useStore } from '../store/graph'

const cols: ColumnSchema[] = [
  { name: 'id', type: 'int', capabilities: [] },
  { name: 'amount', type: 'double', capabilities: [] },
]

describe('canDeclareSchemaKind — which kinds can carry a schema contract', () => {
  it('is true for code ops + any plugin kind', () => {
    for (const k of ['transform', 'vector-search', 'my_plugin_node']) {
      expect(canDeclareSchemaKind(k)).toBe(true)
    }
  })
  it('is false for relational / io / annotation built-ins (never a phantom contract editor)', () => {
    for (const k of ['source', 'filter', 'select', 'sort', 'dedup', 'join', 'sql', 'aggregate',
      'sample', 'metric', 'chart', 'write', 'note', 'section', 'code']) {
      expect(canDeclareSchemaKind(k)).toBe(false)
    }
  })
  it('allows node-wide contracts only for a single effective output', () => {
    expect(canDeclareNodeSchema('my_plugin_node', 1)).toBe(true)
    expect(canDeclareNodeSchema('my_plugin_node', 2)).toBe(false)
  })
})

describe('Inspector — effective named outputs', () => {
  const selectNode = (type: string, outputs: string[] | undefined) => {
    useStore.setState({
      selectedIds: ['node'],
      canvasRole: 'owner',
      doc: {
        id: 'inspector', name: 'Inspector', version: 1, requirements: [], edges: [],
        nodes: [{
          id: 'node', type, position: { x: 0, y: 0 },
          data: { title: type, status: 'draft', history: [], config: outputs ? { outputs } : {} },
        }],
      },
      runs: {}, schemas: { node: null },
    } as any)
  }

  it('shows Section instance ports instead of the static out port', () => {
    register({
      kind: 'section', title: 'section', category: 'compute', inputs: [],
      outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false, blurb: '',
      defaultData: () => ({ title: 'section', status: 'draft', history: [], config: {} }),
    }, () => null)
    selectNode('section', ['left', 'right'])
    render(<Inspector />)
    expect(screen.getByText('left')).toBeInTheDocument()
    expect(screen.getByText('right')).toBeInTheDocument()
    expect(screen.queryByText('out')).not.toBeInTheDocument()
  })

  it('explains why node-wide schema contracts are unavailable for plugin multi-output', () => {
    register({
      kind: 'inspector-multi-plugin', title: 'multi', category: 'compute', inputs: [],
      outputs: [{ id: 'left', wire: 'dataset' }, { id: 'right', wire: 'dataset' }],
      canBypass: false, blurb: '',
      defaultData: () => ({ title: 'multi', status: 'draft', history: [], config: {} }),
    }, () => null)
    selectNode('inspector-multi-plugin', undefined)
    render(<Inspector />)
    expect(screen.getByText(/per-port schema contracts are deferred/i)).toBeInTheDocument()
    expect(screen.queryByText(/Untyped until it runs\. Declare a contract/i)).not.toBeInTheDocument()
    expect(screen.getByText(/Full runs for multi-output nodes are not available yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run' })).toHaveAttribute('aria-disabled', 'true')
  })

  it('keeps edits local, rejects invalid port ids inline, and commits a valid rename on Enter', () => {
    selectNode('section', ['left', 'right'])
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [...state.doc.nodes, {
          id: 'sink', type: 'write', position: { x: 0, y: 0 },
          data: { title: 'write', status: 'draft', history: [], config: {} },
        } as any],
        edges: [{
          id: 'left-sink', source: 'node', sourceHandle: 'left',
          target: 'sink', targetHandle: 'in', data: { wire: 'dataset' },
        }],
      },
    }))
    render(<Inspector />)
    const input = screen.getByDisplayValue('left')

    fireEvent.change(input, { target: { value: '' } })
    expect((useStore.getState().doc.nodes[0].data.config as any).outputs).toEqual(['left', 'right'])
    fireEvent.blur(input)
    expect(screen.getByRole('alert')).toHaveTextContent(/cannot be empty/i)
    expect(useStore.getState().doc.edges).toHaveLength(1)

    fireEvent.change(input, { target: { value: 'right' } })
    fireEvent.blur(input)
    expect(screen.getByRole('alert')).toHaveTextContent(/duplicated.*unique/i)
    expect((useStore.getState().doc.nodes[0].data.config as any).outputs).toEqual(['left', 'right'])

    fireEvent.change(input, { target: { value: 'x'.repeat(129) } })
    fireEvent.blur(input)
    expect(screen.getByRole('alert')).toHaveTextContent(/128 characters or fewer/i)
    expect((useStore.getState().doc.nodes[0].data.config as any).outputs).toEqual(['left', 'right'])

    fireEvent.change(input, { target: { value: 'renamed' } })
    input.focus()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect((useStore.getState().doc.nodes[0].data.config as any).outputs).toEqual(['renamed', 'right'])
    expect(useStore.getState().doc.edges).toEqual([])
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('adds a collision-free port id and disables Add at the 64-port bound', () => {
    selectNode('section', ['out', 'out3'])
    const view = render(<Inspector />)
    fireEvent.click(screen.getByRole('button', { name: /add port/i }))
    expect((useStore.getState().doc.nodes[0].data.config as any).outputs).toEqual(['out', 'out3', 'out4'])

    view.unmount()
    selectNode('section', Array.from({ length: 64 }, (_, index) => `port${index + 1}`))
    render(<Inspector />)
    expect(screen.getByRole('button', { name: /add port/i })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent(/maximum 64 output ports/i)
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
