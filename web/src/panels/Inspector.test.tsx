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
      runs: {}, schemas: { node: { out: null } },
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

  it('defers node-wide schema contracts without blocking a runnable multi-output node', () => {
    register({
      kind: 'inspector-multi-plugin', title: 'multi', category: 'compute', inputs: [],
      outputs: [{ id: 'left', wire: 'dataset' }, { id: 'right', wire: 'dataset' }],
      canBypass: false, blurb: '',
      defaultData: () => ({ title: 'multi', status: 'draft', history: [], config: {} }),
    }, () => null)
    selectNode('inspector-multi-plugin', undefined)
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: [{
          id: 'source', type: 'source', position: { x: 0, y: 0 },
          data: { title: 'source', status: 'draft', history: [], config: { uri: 'events.parquet' } },
        } as any, ...state.doc.nodes],
        edges: [{
          id: 'source-node', source: 'source', target: 'node', data: { wire: 'dataset' },
        }],
      },
    }))
    render(<Inspector />)
    expect(screen.getByText(/per-port schema contracts are deferred/i)).toBeInTheDocument()
    expect(screen.queryByText(/Untyped until it runs\. Declare a contract/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Full runs for multi-output nodes are not available yet/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run' })).toHaveAttribute('aria-disabled', 'false')
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

  it('shows managed create admission and the exact durable revision receipt', () => {
    selectNode('write', undefined)
    useStore.setState({
      runs: { node: {
        phase: 'done',
        writeAdmission: {
          nodeId: 'node', managed: true, destination: '/outputs/output.parquet',
          mode: 'replace', provider: 'managed-local-file', expectedSchema: cols,
          partitions: [], expectedHead: { kind: 'exact', datasetId: 'dataset-1', revisionId: 'rev-1' },
        },
        status: { outputs: [{ writeReceipt: {
          datasetId: 'dataset-1', revisionId: 'rev-2', rows: 2, bytes: 512,
        } }] },
      } },
    } as any)

    render(<Inspector />)

    expect(screen.getByLabelText('Write admission')).toHaveTextContent(/replace.*managed-local-file/i)
    expect(screen.getByLabelText('Write admission')).toHaveTextContent(/expected revision rev-1/i)
    expect(screen.getByLabelText('Write receipt')).toHaveTextContent(/durable revision rev-2/i)
    expect(screen.getByLabelText('Write receipt')).toHaveTextContent(/512 bytes/i)
    expect(screen.queryByText(/^overwrite$/i)).not.toBeInTheDocument()
  })

  it('shows the frozen Lance parent and backend version without a physical path', () => {
    selectNode('write', undefined)
    useStore.setState({
      runs: { node: {
        phase: 'done',
        writeAdmission: {
          nodeId: 'node', managed: true, destination: '/outputs/existing.lance',
          mode: 'append', provider: 'managed-local-lance', expectedSchema: cols,
          partitions: [], expectedHead: { kind: 'exact', datasetId: 'dataset-lance', revisionId: '7' },
        },
        status: { outputs: [{ writeReceipt: {
          datasetId: 'dataset-lance', revisionId: '8', rows: 12, bytes: 1024,
          parentHead: { kind: 'exact', datasetId: 'dataset-lance', revisionId: '7' },
          publication: { backendVersion: '8.0.0' },
        } }] },
      } },
    } as any)

    render(<Inspector />)

    expect(screen.getByLabelText('Write admission')).toHaveTextContent(/append.*managed-local-lance/i)
    expect(screen.getByLabelText('Write admission')).toHaveTextContent(/expected revision 7/i)
    expect(screen.getByLabelText('Write receipt')).toHaveTextContent(/durable revision 8/i)
    expect(screen.getByLabelText('Write receipt')).toHaveTextContent(/parent revision 7/i)
    expect(screen.getByLabelText('Write receipt')).toHaveTextContent(/backend 8\.0\.0/i)
    expect(screen.getByLabelText('Write receipt')).not.toHaveTextContent(/\/outputs\/existing\.lance/i)
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
