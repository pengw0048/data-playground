import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { Inspector, PortRow, canDeclareNodeSchema, canDeclareSchemaKind } from './Inspector'
import type { CanvasEdge, ColumnSchema } from '../types/graph'
import type { CatalogTable } from '../types/api'
import { register } from '../nodes/registry'
import { codeHash } from '../nodes/schema'
import { previewPlanIdentity, useStore } from '../store/graph'
import { api, KernelError } from '../api/client'
import { registerGenericNodes } from '../nodes/generic'
import '../nodes/kinds/source'
import '../nodes/kinds/transform'
import '../nodes/kinds/join'
import '../nodes/kinds/sql'

const cols: ColumnSchema[] = [
  { name: 'id', type: 'int', capabilities: [] },
  { name: 'amount', type: 'double', capabilities: [] },
]

beforeEach(() => {
  useStore.setState({ kernelUp: true, currentUser: null })
})

function registerSpec(kind: string, source?: string) {
  register({
    kind, title: kind, category: 'compute', inputs: [], outputs: [{ id: 'out', wire: 'dataset' }],
    canBypass: false, blurb: '', source,
    defaultData: () => ({ title: kind, status: 'draft', history: [], config: {} }),
  }, () => null)
}

describe('canDeclareSchemaKind — which kinds can carry a schema contract', () => {
  it('is true for code ops + backend-owned plugin kinds', () => {
    registerSpec('my_plugin_node', 'plugin:test')
    for (const k of ['transform', 'vector-search', 'sql', 'my_plugin_node']) {
      expect(canDeclareSchemaKind(k)).toBe(true)
    }
  })
  it('is false for generic built-ins, whether source is builtin or omitted', () => {
    for (const k of ['union', 'window', 'fill', 'unnest', 'unpivot', 'assert']) {
      registerSpec(k, 'builtin')
      expect(canDeclareSchemaKind(k)).toBe(false)
    }
    registerSpec('pivot')
    for (const k of ['pivot', 'unknown_node']) {
      expect(canDeclareSchemaKind(k)).toBe(false)
    }
  })
  it('allows node-wide contracts only for a single effective output', () => {
    expect(canDeclareNodeSchema('my_plugin_node', 1)).toBe(true)
    expect(canDeclareNodeSchema('my_plugin_node', 2)).toBe(false)
  })

  it('uses the existing schema hash contract to detect a changed SQL declaration', () => {
    register({
      kind: 'sql', title: 'sql', category: 'query',
      inputs: [{ id: 'in', wire: 'dataset', multi: true }],
      outputs: [{ id: 'out', wire: 'dataset' }],
      canBypass: false, blurb: '',
      defaultData: () => ({ title: 'sql', status: 'draft', history: [], config: {} }),
    }, () => null)
    useStore.setState({
      selectedIds: ['sql'],
      canvasRole: 'owner',
      doc: {
        id: 'sql-contract', name: 'SQL contract', version: 1, requirements: [], edges: [],
        nodes: [{
          id: 'sql', type: 'sql', position: { x: 0, y: 0 },
          data: {
            title: 'sql', status: 'draft', history: [],
            config: {
              code: 'SELECT owner_id AS copied FROM input',
              sql: 'SELECT owner_id AS actual FROM input',
              outputSchema: [{
                name: 'actual', type: 'string', capabilities: [],
                rowReference: {
                  target: { kind: 'canonical', datasetId: 'stale-target' },
                  keyFields: ['id'],
                  provenance: 'declared',
                },
              }],
              outputSchemaCodeHash: codeHash('SELECT owner_id AS copied FROM input'),
            },
          },
        }],
      },
      runs: {},
      schemas: { sql: { out: [{ name: 'actual', type: 'int', capabilities: [] }] } },
    } as any)

    render(<Inspector />)
    expect(screen.getByText(/Needs review/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Review output schema' }))
    expect(screen.getByText(/SQL changed since this contract was pinned/i)).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Show columns'))
    expect(screen.getByText('actual')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Inspect evidence for actual' }))
    expect(screen.getByTestId('field-evidence-actual')).not.toHaveTextContent('Row-reference target')
    expect(screen.queryByText(/stale-target/i)).not.toBeInTheDocument()
  })

  it('uses the server-derived reference fact for a stale plugin contract', () => {
    register({
      kind: 'stale-contract-plugin', title: 'plugin', category: 'compute',
      inputs: [], outputs: [{ id: 'out', wire: 'dataset' }],
      canBypass: false, blurb: '', source: 'plugin:test',
      defaultData: () => ({ title: 'plugin', status: 'draft', history: [], config: {} }),
    }, () => null)
    useStore.setState({
      selectedIds: ['plugin'],
      canvasRole: 'owner',
      doc: {
        id: 'plugin-contract', name: 'Plugin contract', version: 1, requirements: [], edges: [],
        nodes: [{
          id: 'plugin', type: 'stale-contract-plugin', position: { x: 0, y: 0 },
          data: {
            title: 'plugin', status: 'draft', history: [],
            config: {
              code: 'return current_input',
              outputSchema: [{
                name: 'copied', type: 'int64', capabilities: [],
                rowReference: {
                  target: { kind: 'canonical', datasetId: 'stale-target' },
                  keyFields: ['id'],
                  provenance: 'declared',
                },
              }],
              outputSchemaCodeHash: codeHash('return previous_input'),
            },
          },
        }],
      },
      runs: {},
      schemas: { plugin: { out: [{ name: 'copied', type: 'int64', capabilities: [] }] } },
    } as any)

    render(<Inspector />)
    expect(screen.getByText(/Needs review/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Review output schema' }))
    expect(screen.getByText(/changed since this contract was pinned/i)).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Show columns'))
    fireEvent.click(screen.getByRole('button', { name: 'Inspect evidence for copied' }))
    expect(screen.getByTestId('field-evidence-copied')).not.toHaveTextContent('Row-reference target')
    expect(screen.queryByText(/stale-target/i)).not.toBeInTheDocument()
  })

  it('uses the server schema instead of a forged contract on a conflicting Union', () => {
    register({
      kind: 'union', title: 'union', category: 'compute', source: 'builtin',
      inputs: [{ id: 'in', wire: 'dataset', multi: true }],
      outputs: [{ id: 'out', wire: 'dataset' }], canBypass: false, blurb: '',
      defaultData: () => ({ title: 'union', status: 'draft', history: [], config: {} }),
    }, () => null)
    useStore.setState({
      selectedIds: ['union'],
      canvasRole: 'owner',
      doc: {
        id: 'conflicting-union', name: 'Conflicting Union', version: 1, requirements: [],
        edges: [
          { id: 'left-union', source: 'left', target: 'union', data: { wire: 'dataset' } },
          { id: 'right-union', source: 'right', target: 'union', data: { wire: 'dataset' } },
        ],
        nodes: [
          { id: 'left', type: 'source', position: { x: 0, y: 0 }, data: { title: 'left', status: 'draft', history: [], config: {} } },
          { id: 'right', type: 'source', position: { x: 0, y: 0 }, data: { title: 'right', status: 'draft', history: [], config: {} } },
          {
            id: 'union', type: 'union', position: { x: 0, y: 0 },
            data: {
              title: 'union', status: 'draft', history: [],
              config: {
                outputSchema: [{
                  name: 'owner_id', type: 'string', capabilities: [],
                  rowReference: {
                    target: { kind: 'canonical', datasetId: 'forged-target' },
                    keyFields: ['id'], provenance: 'declared',
                  },
                }],
              },
            },
          },
        ],
      },
      runs: {},
      schemas: { union: { out: [{ name: 'owner_id', type: 'int', capabilities: [] }] } },
    } as any)

    render(<Inspector />)
    fireEvent.click(screen.getByTitle('Show columns'))
    fireEvent.click(screen.getByRole('button', { name: 'Inspect evidence for owner_id' }))
    expect(screen.getByTestId('field-evidence-owner_id')).not.toHaveTextContent('Row-reference target')
    expect(screen.queryByText(/forged-target/i)).not.toBeInTheDocument()
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

  it('uses the exact Library processor description instead of the generic Transform blurb', () => {
    const processorId = `tr_${'b'.repeat(29)}`
    selectNode('transform', undefined)
    useStore.setState((state) => ({
      doc: {
        ...state.doc,
        nodes: state.doc.nodes.map((candidate) => ({
          ...candidate,
          data: {
            ...candidate.data,
            config: {
              source: 'library', processor: processorId, version: 'v1', mode: 'map',
            },
          },
        })),
      },
      processors: [{
        id: processorId, version: 'v1', title: 'Normalize events', mode: 'map',
        category: 'compute', inputColumns: [], inputSchema: [], outputSchema: [],
        requirements: [], paramsSchema: {}, previewable: true,
        blurb: 'Normalizes event locations for downstream training.',
        provenance: 'promoted',
      }],
      canvasTransformReferences: [],
    } as any))

    render(<Inspector />)

    expect(screen.getByTitle('Normalizes event locations for downstream training.')).toBeInTheDocument()
    expect(screen.queryByText('Apply a Python transform to rows')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open processor definition' })).toBeVisible()
    expect(screen.getByText(`${processorId}@v1`)).toBeInTheDocument()
    expect(screen.queryByText('(empty)')).not.toBeInTheDocument()
  })

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

  it('omits unavailable node-wide schema controls without blocking a runnable multi-output node', () => {
    register({
      kind: 'inspector-multi-plugin', title: 'multi', category: 'compute', inputs: [],
      outputs: [{ id: 'left', wire: 'dataset' }, { id: 'right', wire: 'dataset' }],
      canBypass: false, blurb: '', source: 'plugin:test',
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
    expect(screen.queryByText(/per-port schema contracts are deferred/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Node-wide schema contracts are unavailable/i)).not.toBeInTheDocument()
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

  it.each([
    ['create', 'Create a new dataset'],
    ['replace', 'Replace the selected dataset'],
    ['append', 'Append to the selected dataset'],
  ] as const)('puts %s Write mode behind the task-first default hierarchy', (mode, label) => {
    selectNode('write', undefined)
    useStore.setState({
      runs: { node: {
        phase: 'estimated',
        writeAdmission: {
          nodeId: 'node', managed: true, destination: '/outputs/output.parquet',
          mode, provider: 'managed-local-file', expectedSchema: cols,
          partitions: [], expectedHead: { kind: 'exact', datasetId: 'dataset-1', revisionId: 'rev-1' },
        }, status: { outputs: [] },
      } },
    } as any)

    render(<Inspector />)

    const publication = screen.getByLabelText('Write publication')
    expect(publication).toHaveTextContent('Dataset name')
    expect(publication).toHaveTextContent('Workspace outputs')
    expect(publication).toHaveTextContent(label)
    expect(publication).toHaveTextContent('Ready to run')
    expect(screen.getByText('Technical details').closest('details')).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText('Technical details'))
    expect(publication).toHaveTextContent('managed-local-file')
    expect(publication).toHaveTextContent('dataset-1@rev-1')
  })

  it('hands destination management to the Canvas Settings destinations pane', async () => {
    selectNode('write', undefined)
    useStore.setState({
      currentUser: { id: 'admin', name: 'Admin', capabilities: ['global_settings'] },
    })
    vi.spyOn(api, 'destinations').mockResolvedValue({
      destinations: [{ id: 'outputs', name: 'Workspace outputs', backend: 'local', root: '/workspace/outputs' }],
      backends: ['local', 's3', 'gs'],
    })
    const onOpenSettings = vi.fn()
    window.addEventListener('dp-open-settings', onOpenSettings)

    render(<Inspector />)
    fireEvent.click(screen.getByRole('button', { name: 'Choose destination…' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Manage destinations' }))

    expect(onOpenSettings).toHaveBeenCalledTimes(1)
    const event = onOpenSettings.mock.calls[0][0] as CustomEvent<{ category: string }>
    expect(event.detail.category).toBe('destinations')
    await waitFor(() => expect(screen.queryByText('Choose output destination')).not.toBeInTheDocument())
    window.removeEventListener('dp-open-settings', onOpenSettings)
  })

  it('shows configured destinations without exposing Settings to an editor', async () => {
    selectNode('write', undefined)
    useStore.setState({
      currentUser: { id: 'editor', name: 'Editor', capabilities: [] },
    })
    vi.spyOn(api, 'destinations').mockResolvedValue({
      destinations: [{ id: 'outputs', name: 'Workspace outputs', backend: 'local', root: '/workspace/outputs' }],
      backends: ['local', 's3', 'gs'],
    })
    const onOpenSettings = vi.fn()
    window.addEventListener('dp-open-settings', onOpenSettings)

    render(<Inspector />)
    fireEvent.click(screen.getByRole('button', { name: 'Choose destination…' }))

    expect((await screen.findAllByRole('button', { name: 'Workspace outputs' }))[0]).toBeVisible()
    expect(screen.getByText('Dataset name')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Manage destinations' })).not.toBeInTheDocument()
    expect(onOpenSettings).not.toHaveBeenCalled()
    window.removeEventListener('dp-open-settings', onOpenSettings)
  })

  it('keeps current admission blockers visible before publication', () => {
    selectNode('write', undefined)
    useStore.setState({
      runs: { node: {
        phase: 'estimated',
        writeAdmission: {
          nodeId: 'node', managed: true, destination: '/outputs/existing.lance',
          mode: 'append', provider: 'managed-local-lance', expectedSchema: cols,
          partitions: [], expectedHead: { kind: 'exact', datasetId: 'dataset-lance', revisionId: '7' }, blocker: 'the upstream transform “Normalize purchases” does not have a bounded output schema contract. Select the upstream transform “Normalize purchases”, then in the Inspector choose Output schema (contract) → Infer from sample.',
        },
        status: { outputs: [] },
      } },
    } as any)

    render(<Inspector />)

    expect(screen.getByLabelText('Write blocker')).toHaveTextContent('Fix before running: the upstream transform “Normalize purchases” does not have a bounded output schema contract. Select the upstream transform “Normalize purchases”, then in the Inspector choose Output schema (contract) → Infer from sample.')
    expect(screen.queryByRole('link', { name: 'Open dataset' })).not.toBeInTheDocument()
  })

  it('keeps the completed admission and exact receipt above a blocked next admission', () => {
    selectNode('write', undefined)
    const outcomeAdmission = {
      nodeId: 'node', managed: true, destination: '/outputs/existing.lance',
      mode: 'append', provider: 'managed-local-lance', expectedSchema: cols,
      partitions: [], expectedHead: { kind: 'exact', datasetId: 'dataset-lance', revisionId: '7' },
    }
    useStore.setState({
      runs: { node: {
        phase: 'done',
        writeAdmission: { ...outcomeAdmission, mode: 'replace', blocker: 'the next destination head moved' },
        writeOutcomeAdmission: outcomeAdmission,
        status: { outputs: [{ writeReceipt: {
          datasetId: 'dataset-lance', revisionId: '8', rows: 12, bytes: 1024,
          durable: true, head: { datasetId: 'dataset-lance', revisionId: '8', retentionOwner: 'core' },
          schema: cols, partitions: [],
          parentHead: { kind: 'exact', datasetId: 'dataset-lance', revisionId: '7' },
          publication: { provider: 'managed-local-lance', logicalUri: 'managed://dataset-lance', artifactUri: 'file:///dataset-lance', publishSequence: 8, idempotencyKey: 'write-8', backendVersion: '8.0.0' },
        } }] },
      } },
    } as any)

    render(<Inspector />)

    expect(screen.queryByLabelText('Write blocker')).not.toBeInTheDocument()
    expect(screen.getByText('Mode').parentElement).toHaveTextContent('Append to the selected dataset')
    expect(screen.getByRole('link', { name: 'Open dataset' })).toBeVisible()
    fireEvent.click(screen.getByText('Technical details'))
    expect(screen.getByLabelText('Write publication')).toHaveTextContent('dataset-lance@8')
    expect(screen.getByLabelText('Write publication')).toHaveTextContent('dataset-lance@7')
  })

  it('does not invent an exact result when no receipt exists', () => {
    selectNode('write', undefined)
    useStore.setState({ runs: { node: {
      phase: 'done', writeAdmission: {
        nodeId: 'node', managed: true, destination: '/outputs/unknown.parquet', mode: 'replace',
        provider: 'managed-local-file', expectedSchema: cols, partitions: [],
      }, status: { outputs: [] },
    } } } as any)
    render(<Inspector />)
    expect(screen.queryByRole('link', { name: 'Open dataset' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Write publication')).toHaveTextContent('Run finished, but the published dataset could not be confirmed.')
  })

  it('keeps merge and upsert controls out of the ordinary Write flow until Advanced opens', () => {
    selectNode('write', undefined)
    render(<Inspector />)
    const advanced = screen.getByText('Advanced write operations').closest('details')
    expect(advanced).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText('Advanced write operations'))
    expect(advanced).toHaveAttribute('open')
    expect(screen.getByLabelText('Certified column merge')).toBeInTheDocument()
  })
})

describe('Inspector — advanced execution', () => {
  const selectTransform = (config: Record<string, unknown>) => {
    useStore.setState({
      selectedIds: ['transform'], canvasRole: 'owner', runs: {}, schemas: {},
      doc: { id: 'execution', version: 1, requirements: [], edges: [], nodes: [{
        id: 'transform', type: 'transform', position: { x: 0, y: 0 },
        data: { title: 'transform', status: 'draft', history: [], config },
      }] },
    } as any)
  }

  it('keeps an unconfigured Transform free of execution controls until Advanced execution opens', () => {
    selectTransform({})
    render(<Inspector />)

    const advanced = screen.getByText('Advanced execution').closest('details')
    expect(advanced).not.toHaveAttribute('open')
    expect(screen.getByText('Resources (placement)')).not.toBeVisible()
    expect(screen.getByText('Materialization')).not.toBeVisible()

    fireEvent.click(screen.getByText('Advanced execution'))
    expect(advanced).toHaveAttribute('open')
    expect(screen.getByText('Resources (placement)')).toBeVisible()
    expect(screen.getByText('Materialization')).toBeVisible()
  })

  it('keeps configured resources visible as a summary and edits the existing requirements payload', () => {
    selectTransform({ requires: { gpu: 8, gpuType: 'a100', cpu: 4 } })
    render(<Inspector />)

    expect(screen.getByText('Resources').parentElement).toHaveTextContent('8 GPUs · a100 · 4 CPUs')
    fireEvent.click(screen.getByRole('button', { name: 'Edit resources' }))
    const gpu = screen.getByLabelText('GPUs') as HTMLInputElement
    expect(gpu).toHaveValue(8)
    fireEvent.change(gpu, { target: { value: '4' } })
    expect((useStore.getState().doc.nodes[0].data.config as any).requires).toEqual({ gpu: 4, gpuType: 'a100', cpu: 4 })
  })
})

describe('Inspector — output schema disclosure', () => {
  const selectTransform = (config: Record<string, unknown>) => {
    useStore.setState({
      selectedIds: ['transform'], canvasRole: 'owner', runs: {}, schemas: {},
      doc: { id: 'output-schema', version: 1, requirements: [], edges: [], nodes: [{
        id: 'transform', type: 'transform', position: { x: 0, y: 0 },
        data: { title: 'transform', status: 'draft', history: [], config },
      }] },
    } as any)
  }

  it('keeps an unconfigured contract under Advanced output schema', () => {
    selectTransform({})
    render(<Inspector />)

    const advanced = screen.getByText('Advanced output schema').closest('details')
    expect(advanced).not.toHaveAttribute('open')
    expect(screen.getByText('Output schema (contract)')).not.toBeVisible()
    expect(screen.queryByRole('button', { name: 'Edit output schema' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('Advanced output schema'))
    expect(advanced).toHaveAttribute('open')
    expect(screen.getByText('Untyped until it runs. Declare a contract, infer it, or reference a named one. Leave empty to stay dynamic.')).toBeVisible()
  })

  it('summarizes a configured contract and opens its editor directly', () => {
    selectTransform({ outputSchema: [{ name: 'clean_id', type: 'int', capabilities: [] }] })
    render(<Inspector />)

    expect(screen.getByText('1 declared column')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Edit output schema' }))
    expect(screen.getByDisplayValue('clean_id')).toBeVisible()
  })

  it('keeps a stale contract discoverable and directly reviewable', () => {
    selectTransform({
      code: 'return current_input',
      outputSchema: [{ name: 'clean_id', type: 'int', capabilities: [] }],
      outputSchemaCodeHash: codeHash('return previous_input'),
    })
    render(<Inspector />)

    expect(screen.getByText(/Needs review/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Review output schema' }))
    expect(screen.getByText(/cell changed since this contract was pinned/i)).toBeVisible()
    expect(screen.getByDisplayValue('clean_id')).toBeVisible()
  })

  it('keeps configured summaries informative without offering viewer-only edit actions', () => {
    selectTransform({
      code: 'return current_input',
      requires: { gpu: 8, gpuType: 'a100' },
      checkpoint: true,
      outputSchema: [{ name: 'clean_id', type: 'int', capabilities: [] }],
      outputSchemaCodeHash: 'outdated-contract-hash',
    })
    useStore.setState({ canvasRole: 'viewer' })
    render(<Inspector />)

    expect(screen.getByText(/8 GPUs · a100/)).toBeVisible()
    expect(screen.getByText(/Checkpointed output/)).toBeVisible()
    expect(screen.getByText(/Needs review/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Edit resources' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit materialization' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Review output schema' })).not.toBeInTheDocument()
  })
})

describe('Inspector — output schema inference input', () => {
  const bindings = [{ name: 'threshold', value: 10 }]
  const inferred = {
    columns: cols, rows: [[1, 12.5]], truncated: false, completeness: 'page',
  }

  const selectNode = (type: 'transform' | 'sql') => {
    const nodeId = type
    const doc = {
      id: `schema-infer-${type}`, version: 1, requirements: [],
      nodes: [
        {
          id: 'source', type: 'source', position: { x: 0, y: 0 },
          data: {
            title: 'events', status: 'latest', history: [],
            config: { uri: 'events.parquet' },
          },
        },
        {
          id: nodeId, type, position: { x: 0, y: 0 },
          data: {
            title: type, status: 'draft', history: [],
            config: type === 'transform'
              ? { source: 'adhoc', mode: 'map', code: 'def fn(row): return row' }
              : { sql: 'SELECT * FROM input' },
          },
        },
      ],
      edges: [{
        id: `source-${nodeId}`, source: 'source', sourceHandle: 'out',
        target: nodeId, targetHandle: 'in', data: { wire: 'dataset' as const },
      }],
    }
    useStore.setState({
      selectedIds: [nodeId], canvasRole: 'owner', schemas: {}, previews: {},
      runs: { [nodeId]: { phase: 'idle', parameterBindings: bindings } }, doc,
    } as any)
    return doc as any
  }

  const openAndInfer = () => {
    render(<Inspector />)
    fireEvent.click(screen.getByText('Advanced output schema'))
    fireEvent.click(screen.getByRole('button', { name: 'Infer from sample' }))
  }

  beforeEach(() => {
    vi.spyOn(api, 'listSchemas').mockResolvedValue([])
    vi.spyOn(api, 'plan').mockResolvedValue({ regions: [] })
  })

  afterEach(() => vi.restoreAllMocks())

  it('infers a Transform schema from the server-owned retained editor preview', async () => {
    const doc = selectNode('transform')
    const retained = vi.spyOn(api, 'retainedEditorPreview').mockResolvedValue(inferred as any)
    const ordinary = vi.spyOn(api, 'preview')

    openAndInfer()

    await waitFor(() => expect(
      useStore.getState().doc.nodes.find((node) => node.id === 'transform')?.data.config.outputSchema,
    ).toEqual(cols))
    expect(retained).toHaveBeenCalledWith(
      doc, 'transform', 50, 0, undefined, bindings,
    )
    expect(ordinary).not.toHaveBeenCalled()
  })

  it.each([
    'retained_upstream_unavailable',
    'retained_upstream_stale',
    'retained_upstream_expired',
  ])('falls back to ordinary preview for %s', async (code) => {
    const doc = selectNode('transform')
    vi.spyOn(api, 'retainedEditorPreview').mockRejectedValue(
      new KernelError(409, 'retained input unavailable', code),
    )
    const ordinary = vi.spyOn(api, 'preview').mockResolvedValue(inferred as any)

    openAndInfer()

    await waitFor(() => expect(
      useStore.getState().doc.nodes.find((node) => node.id === 'transform')?.data.config.outputSchema,
    ).toEqual(cols))
    expect(ordinary).toHaveBeenCalledWith(
      doc, 'transform', 50, 0, undefined, undefined, bindings,
    )
  })

  it('does not hide a non-retained error behind ordinary preview', async () => {
    selectNode('transform')
    vi.spyOn(api, 'retainedEditorPreview').mockRejectedValue(
      new KernelError(403, 'retained input access denied', 'permission_denied'),
    )
    const ordinary = vi.spyOn(api, 'preview')

    openAndInfer()

    expect(await screen.findByText(/retained input access denied/)).toBeVisible()
    expect(ordinary).not.toHaveBeenCalled()
    expect(useStore.getState().doc.nodes.find(
      (node) => node.id === 'transform',
    )?.data.config.outputSchema).toBeUndefined()
  })

  it('keeps non-Transform inference on ordinary preview', async () => {
    const doc = selectNode('sql')
    const retained = vi.spyOn(api, 'retainedEditorPreview')
    const ordinary = vi.spyOn(api, 'preview').mockResolvedValue(inferred as any)

    openAndInfer()

    await waitFor(() => expect(
      useStore.getState().doc.nodes.find((node) => node.id === 'sql')?.data.config.outputSchema,
    ).toEqual(cols))
    expect(retained).not.toHaveBeenCalled()
    expect(ordinary).toHaveBeenCalledWith(
      doc, 'sql', 50, 0, undefined, undefined, bindings,
    )
  })

  it('does not install a retained result after parameter bindings change', async () => {
    selectNode('transform')
    let finish!: (result: typeof inferred) => void
    const retained = vi.spyOn(api, 'retainedEditorPreview').mockImplementation(
      () => new Promise((resolve) => { finish = resolve }),
    )
    vi.spyOn(api, 'preview')

    openAndInfer()
    await waitFor(() => expect(retained).toHaveBeenCalled())
    act(() => useStore.setState((state) => ({
      runs: {
        ...state.runs,
        transform: {
          ...state.runs.transform,
          parameterBindings: [{ name: 'threshold', value: 11 }],
        },
      },
    })))
    await act(async () => { finish(inferred); await Promise.resolve() })

    expect(await screen.findByText(/parameter bindings changed while the sample was loading/i)).toBeVisible()
    expect(useStore.getState().doc.nodes.find(
      (node) => node.id === 'transform',
    )?.data.config.outputSchema).toBeUndefined()
  })
})

describe('Inspector — observed output schema', () => {
  const selectTransform = (
    config: Record<string, unknown> = {}, schema: ColumnSchema[] | null = null,
  ) => {
    const doc = {
      id: 'observed-output', version: 1, requirements: [], edges: [], nodes: [{
        id: 'transform', type: 'transform', position: { x: 0, y: 0 },
        data: { title: 'transform', status: 'draft', history: [], config },
      }],
    }
    useStore.setState({
      selectedIds: ['transform'], canvasRole: 'owner', runs: {}, schemas: { transform: { out: schema } }, previews: {}, doc,
    } as any)
    return doc as any
  }

  const observedPreview = (
    doc: any, portId = 'out', planIdentity = previewPlanIdentity(doc, 'transform', portId),
    result: { columns: ColumnSchema[]; error?: string; notPreviewable?: boolean } = { columns: cols },
  ) => ({
    canvasId: doc.id, nodeId: 'transform', portId, planIdentity, requestGeneration: 1,
    result,
  })

  it('keeps a dynamic Transform output untyped before a current result exists', () => {
    selectTransform()
    render(<Inspector />)
    expect(screen.getByText('untyped')).toBeVisible()
  })

  it('shows columns observed from a current matching result', () => {
    const doc = selectTransform({}, [{ name: 'server_only', type: 'string', capabilities: [] }])
    useStore.setState({ previews: { transform: observedPreview(doc) } } as any)
    render(<Inspector />)
    fireEvent.click(screen.getByTitle('Show columns'))
    expect(screen.getByText('id')).toBeVisible()
    expect(screen.getByText('amount')).toBeVisible()
    expect(screen.queryByText('server_only')).not.toBeInTheDocument()
    expect(screen.getByText('2 cols')).toBeVisible()
  })

  it('does not use stale or mismatched-port results as output schema evidence', () => {
    const fallback = [{ name: 'server_only', type: 'string', capabilities: [] }]
    const staleDoc = selectTransform({}, fallback)
    useStore.setState({ previews: { transform: observedPreview(staleDoc, 'out', 'stale-plan') } } as any)
    const staleView = render(<Inspector />)
    fireEvent.click(screen.getByTitle('Show columns'))
    expect(screen.getByText('server_only')).toBeVisible()
    expect(screen.queryByText('amount')).not.toBeInTheDocument()
    staleView.unmount()

    const mismatchedDoc = selectTransform({}, fallback)
    useStore.setState({ previews: { transform: observedPreview(mismatchedDoc, 'other') } } as any)
    render(<Inspector />)
    fireEvent.click(screen.getByTitle('Show columns'))
    expect(screen.getByText('server_only')).toBeVisible()
    expect(screen.queryByText('amount')).not.toBeInTheDocument()
  })

  it.each([
    ['errored', { error: 'preview failed' }, [{ name: 'server_only', type: 'string', capabilities: [] }], 'server_only'],
    ['refused', { notPreviewable: true }, null, 'untyped'],
  ] as const)('does not use columns attached to a %s result', (_label, resultState, fallback, expected) => {
    const doc = selectTransform({}, fallback)
    useStore.setState({ previews: {
      transform: observedPreview(
        doc, 'out', previewPlanIdentity(doc, 'transform', 'out'),
        { columns: cols, ...resultState },
      ),
    } } as any)
    render(<Inspector />)

    if (expected === 'untyped') {
      expect(screen.getByText('untyped')).toBeVisible()
    } else {
      fireEvent.click(screen.getByTitle('Show columns'))
      expect(screen.getByText(expected)).toBeVisible()
    }
    expect(screen.queryByText('amount')).not.toBeInTheDocument()
  })

  it('keeps a current explicit declared contract ahead of observed columns', () => {
    const declared = [{ name: 'declared_id', type: 'string', capabilities: [] }]
    const doc = selectTransform(
      { code: 'return current_input', outputSchema: declared, outputSchemaCodeHash: codeHash('return current_input') },
      [{ name: 'server_only', type: 'string', capabilities: [] }],
    )
    useStore.setState({ previews: { transform: observedPreview(doc) } } as any)
    render(<Inspector />)
    fireEvent.click(screen.getByTitle('Show columns'))
    expect(screen.getByText('declared_id')).toBeVisible()
    expect(screen.queryByText('amount')).not.toBeInTheDocument()
    expect(screen.queryByText('server_only')).not.toBeInTheDocument()
    expect(screen.getByText('1 cols')).toBeVisible()
  })
})

describe('Inspector — linear checkpoint availability', () => {
  it('enables a checkpoint only on the supported Source → Select → Write route', () => {
    const source = { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', history: [], config: {} } }
    const select = { id: 'select', type: 'select', position: { x: 0, y: 0 }, data: { title: 'select', status: 'draft', history: [], config: { select: '*' } } }
    const write = { id: 'write', type: 'write', position: { x: 0, y: 0 }, data: { title: 'write', status: 'draft', history: [], config: {} } }
    useStore.setState({
      selectedIds: ['select'], canvasRole: 'owner', runs: {}, schemas: {},
      doc: {
        id: 'checkpoint', version: 1, requirements: [], nodes: [source, select, write],
        edges: [
          { id: 'source-select', source: 'source', sourceHandle: 'out', target: 'select', targetHandle: 'in' },
          { id: 'select-write', source: 'select', sourceHandle: 'out', target: 'write', targetHandle: 'in' },
        ],
      },
    } as any)

    render(<Inspector />)
    fireEvent.click(screen.getByText('Advanced execution'))
    const toggle = screen.getByTestId('checkpoint-toggle')
    expect(toggle).toBeEnabled()
    fireEvent.click(toggle)
    expect((useStore.getState().doc.nodes.find((node) => node.id === 'select')?.data.config as any).checkpoint).toBe(true)
    expect(screen.getByRole('button', { name: 'Edit materialization' }).parentElement).toHaveTextContent('Checkpointed output')
  })

  it('disables an unsupported checkpoint where a researcher encounters it', () => {
    const source = { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', history: [], config: {} } }
    const filter = { id: 'filter', type: 'filter', position: { x: 0, y: 0 }, data: { title: 'filter', status: 'draft', history: [], config: {} } }
    const transform = { id: 'transform', type: 'transform', position: { x: 0, y: 0 }, data: { title: 'transform', status: 'draft', history: [], config: {} } }
    useStore.setState({
      selectedIds: ['transform'], canvasRole: 'owner', runs: {}, schemas: {},
      doc: {
        id: 'checkpoint', version: 1, requirements: [], nodes: [source, filter, transform],
        edges: [
          { id: 'source-filter', source: 'source', target: 'filter' },
          { id: 'filter-transform', source: 'filter', target: 'transform' },
        ],
      },
    } as any)

    render(<Inspector />)
    fireEvent.click(screen.getByText('Advanced execution'))
    expect(screen.getByTestId('checkpoint-toggle')).toBeDisabled()
    expect(screen.getByText('Checkpoints are available only for Source → Select → Write.')).toBeInTheDocument()
  })

  it.each([
    ['a wrong source handle', {
      nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', history: [], config: {} } },
        { id: 'select', type: 'select', position: { x: 0, y: 0 }, data: { title: 'select', status: 'draft', history: [], config: { select: '*' } } },
        { id: 'write', type: 'write', position: { x: 0, y: 0 }, data: { title: 'write', status: 'draft', history: [], config: {} } },
      ],
      edges: [
        { id: 'source-select', source: 'source', sourceHandle: 'preview', target: 'select', targetHandle: 'in' },
        { id: 'select-write', source: 'select', sourceHandle: 'out', target: 'write', targetHandle: 'in' },
      ],
    }],
    ['a wrong target', {
      nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', history: [], config: {} } },
        { id: 'select', type: 'select', position: { x: 0, y: 0 }, data: { title: 'select', status: 'draft', history: [], config: { select: '*' } } },
        { id: 'write', type: 'write', position: { x: 0, y: 0 }, data: { title: 'write', status: 'draft', history: [], config: {} } },
      ],
      edges: [
        { id: 'source-write', source: 'source', sourceHandle: 'out', target: 'write', targetHandle: 'in' },
        { id: 'select-write', source: 'select', sourceHandle: 'out', target: 'write', targetHandle: 'in' },
      ],
    }],
    ['a disabled source', {
      nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', history: [], disabled: true, config: {} } },
        { id: 'select', type: 'select', position: { x: 0, y: 0 }, data: { title: 'select', status: 'draft', history: [], config: { select: '*' } } },
        { id: 'write', type: 'write', position: { x: 0, y: 0 }, data: { title: 'write', status: 'draft', history: [], config: {} } },
      ],
      edges: [
        { id: 'source-select', source: 'source', sourceHandle: 'out', target: 'select', targetHandle: 'in' },
        { id: 'select-write', source: 'select', sourceHandle: 'out', target: 'write', targetHandle: 'in' },
      ],
    }],
    ['a bypassed write', {
      nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', history: [], config: {} } },
        { id: 'select', type: 'select', position: { x: 0, y: 0 }, data: { title: 'select', status: 'draft', history: [], config: { select: '*' } } },
        { id: 'write', type: 'write', position: { x: 0, y: 0 }, data: { title: 'write', status: 'draft', history: [], bypassed: true, config: {} } },
      ],
      edges: [
        { id: 'source-select', source: 'source', sourceHandle: 'out', target: 'select', targetHandle: 'in' },
        { id: 'select-write', source: 'select', sourceHandle: 'out', target: 'write', targetHandle: 'in' },
      ],
    }],
    ['another checkpoint flag', {
      nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { title: 'source', status: 'draft', history: [], config: {} } },
        { id: 'select', type: 'select', position: { x: 0, y: 0 }, data: { title: 'select', status: 'draft', history: [], config: { select: '*' } } },
        { id: 'write', type: 'write', position: { x: 0, y: 0 }, data: { title: 'write', status: 'draft', history: [], config: { checkpoint: true } } },
      ],
      edges: [
        { id: 'source-select', source: 'source', sourceHandle: 'out', target: 'select', targetHandle: 'in' },
        { id: 'select-write', source: 'select', sourceHandle: 'out', target: 'write', targetHandle: 'in' },
      ],
    }],
  ])('disables a checkpoint for %s', (_case, doc) => {
    useStore.setState({
      selectedIds: ['select'], canvasRole: 'owner', runs: {}, schemas: {},
      doc: { id: 'checkpoint', version: 1, requirements: [], ...doc },
    } as any)

    render(<Inspector />)
    fireEvent.click(screen.getByText('Advanced execution'))
    expect(screen.getByTestId('checkpoint-toggle')).toBeDisabled()
  })
})

describe('Inspector — Source connection details', () => {
  it('keeps opaque Source bindings and field evidence out of the Canvas card surface until requested', async () => {
    const exact = vi.spyOn(api, 'datasetRevision').mockResolvedValue({
      datasetId: 'provider:dataset:an-intentionally-long-opaque-identity',
      revisionId: 'revision:an-intentionally-long-opaque-identity', retentionOwner: 'provider',
      summary: { rowCount: 100 }, preview: {
        columns: [{ name: 'customer_id', type: 'int64', capabilities: [], annotations: [{
          key: 'provider.note', value: 'selected exact schema', encoding: 'utf8', provenance: 'provider',
        }] }], rows: [], hasMore: false, rowLimit: 100,
      },
    } as any)
    useStore.setState({
      selectedIds: ['source'], canvasRole: 'owner', runs: {}, schemas: {},
      catalog: [], doc: { id: 'source-connection', name: 'Source connection', version: 1, requirements: [], edges: [],
        nodes: [{ id: 'source', type: 'source', position: { x: 0, y: 0 }, data: {
          title: 'orders', status: 'latest', history: [], config: {
            providerName: 'Luma Data API', providerResourceRef: 'provider://datasets/orders',
            providerMountId: 'mount:very-long-provider-mount', providerSourceBindingId: 'binding:very-long-provider-source-binding',
            datasetRef: { kind: 'exact', datasetId: 'provider:dataset:an-intentionally-long-opaque-identity', revisionId: 'revision:an-intentionally-long-opaque-identity' },
          },
        } }],
      },
    } as any)

    render(<Inspector />)
    expect(screen.getByText('binding:very-long-provider-source-binding')).not.toBeVisible()
    expect(screen.queryByText(/Field evidence/i)).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('Connection details'))
    const details = await screen.findByLabelText('Source connection details')
    expect(details).toHaveTextContent('provider://datasets/orders')
    expect(details).toHaveTextContent('binding:very-long-provider-source-binding')
    expect(details).toHaveTextContent('revision:an-intentionally-long-opaque-identity')
    expect(await screen.findByText('Field evidence · 1 column')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Inspect evidence for customer_id' }))
    expect(await screen.findByTestId('field-evidence-customer_id')).toHaveTextContent('selected exact schema')
    expect(exact).toHaveBeenCalledWith('provider:dataset:an-intentionally-long-opaque-identity', 'revision:an-intentionally-long-opaque-identity')
    exact.mockRestore()
  })
})

describe('Inspector — draft Source entry', () => {
  beforeEach(() => {
    registerGenericNodes([{
      kind: 'source', title: 'source', category: 'io', tag: 'dataset',
      inputs: [], outputs: [{ id: 'out', wire: 'dataset' }],
      params: [
        { name: 'uri', type: 'string', label: 'dataset uri' },
        { name: 'delimiter', type: 'string', label: 'CSV delimiter (blank=auto)' },
        { name: 'header', type: 'select', options: ['auto', 'yes', 'no'], default: 'auto', label: 'CSV header row' },
      ],
      canBypass: false, previewable: true, blurb: 'Choose a registered dataset',
    }])
  })

  const selectSource = (config: Record<string, unknown>, catalog: CatalogTable[] = []) => {
    useStore.setState({
      selectedIds: ['source'], canvasRole: 'owner', runs: {}, schemas: {}, catalog,
      doc: { id: 'source-entry', name: 'Source entry', version: 1, requirements: [], edges: [],
        nodes: [{ id: 'source', type: 'source', position: { x: 0, y: 0 }, data: {
          title: 'Source', status: 'draft', history: [], config,
        } }],
      },
    } as any)
  }

  it('leads an unbound Source with its three entry actions and keeps unavailable controls out', () => {
    selectSource({})
    const events: string[] = []
    const receive = (event: Event) => events.push((event as CustomEvent<string>).detail)
    window.addEventListener('dataplay:source-entry:source', receive)
    render(<Inspector />)

    expect(screen.getByText('Choose data')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Select dataset' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Upload a file…' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Register or browse an accessible path…' })).toBeInTheDocument()
    expect(screen.getByText('Connection details')).not.toBeVisible()
    expect(screen.queryByText('Related data')).not.toBeInTheDocument()
    expect(screen.queryByText('Ports')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'View data' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Count rows' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Disable' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Duplicate' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Select dataset' }))
    fireEvent.click(screen.getByRole('button', { name: 'Upload a file…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Register or browse an accessible path…' }))
    expect(events).toEqual(['select', 'upload', 'browse'])
    window.removeEventListener('dataplay:source-entry:source', receive)
  })

  it('reveals CSV parsing only after a draft becomes a manual delimited-text URI', () => {
    selectSource({})
    render(<Inspector />)
    const before = JSON.stringify(useStore.getState().doc.nodes[0].data.config)
    expect(screen.getByLabelText('Dataset URI')).not.toBeVisible()
    fireEvent.click(screen.getByText('Advanced source configuration'))
    expect(screen.getByLabelText('Dataset URI')).toBeVisible()
    expect(screen.queryByLabelText('CSV delimiter')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('CSV header row')).not.toBeInTheDocument()
    expect(JSON.stringify(useStore.getState().doc.nodes[0].data.config)).toBe(before)
    fireEvent.change(screen.getByLabelText('Dataset URI'), {
      target: { value: 'file:///data/manual-input.csv' },
    })
    expect(screen.getByLabelText('CSV delimiter')).toBeVisible()
    expect(screen.getByLabelText('CSV header row')).toBeVisible()
  })

  it('keeps focus while entering a manual URI, then restores the configured Source Inspector', async () => {
    selectSource({})
    render(<Inspector />)
    fireEvent.click(screen.getByText('Advanced source configuration'))
    const uri = screen.getByLabelText('Dataset URI')
    uri.focus()
    fireEvent.change(uri, { target: { value: 'events.parquet' } })

    expect(uri).toHaveFocus()
    expect(uri).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Count rows' })).toBeInTheDocument()
    fireEvent.blur(uri)
    await waitFor(() => expect(screen.getByText('Properties')).toBeInTheDocument())
    expect(screen.getByText('Data source')).toBeInTheDocument()
    expect(screen.queryByText(/CSV delimiter/)).not.toBeInTheDocument()
    expect(screen.queryByText('CSV header row')).not.toBeInTheDocument()
    expect(useStore.getState().doc.nodes[0].data.config.uri).toBe('events.parquet')
  })

  it('shows the real Workspace local Source as bound without raw URI or CSV controls', () => {
    const table: CatalogTable = {
      id: 'events', registrationId: 'dataset:events', name: 'events',
      uri: 'file:///workspace/events.parquet', rowCount: 2000, columns: cols,
    }
    selectSource({
      uri: table.uri, tableId: table.id, registrationId: table.registrationId,
    }, [table])
    render(<Inspector />)
    expect(screen.getByTitle('Local catalog · Current version · 2,000 rows · 2 columns')).toBeInTheDocument()
    expect(screen.queryByTitle('Choose a registered dataset')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Dataset URI')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('CSV delimiter')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('CSV header row')).not.toBeInTheDocument()
    expect(screen.getByText('Data source')).toBeInTheDocument()
    expect(screen.getByText('Related data')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Connection details'))
    expect(screen.getByLabelText('Source connection details')).toHaveTextContent('dataset:events')
  })

  it('shows the real Workspace provider exact Source as bound without manual parsing controls', () => {
    selectSource({
      uri: 'provider+dataset://mount/source-binding',
      providerResourceRef: 'dataset:external/orders',
      providerMountId: 'mount',
      providerSourceBindingId: 'source-binding',
      providerName: 'Luma Data API',
      providerReadMode: 'exact',
      datasetRef: {
        kind: 'exact',
        datasetId: 'provider-dataset-identity',
        revisionId: 'provider-revision-7',
      },
    })
    render(<Inspector />)
    expect(screen.getByTitle('Luma Data API · Exact version provider-revision-7')).toBeInTheDocument()
    expect(screen.queryByTitle('Choose a registered dataset')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Dataset URI')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('CSV delimiter')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('CSV header row')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Connection details'))
    const details = screen.getByLabelText('Source connection details')
    expect(details).toHaveTextContent('provider-dataset-identity')
    expect(details).toHaveTextContent('provider-revision-7')
  })

  it.each([
    ['exact', {
      uri: 'file:///data/exact.csv',
      datasetRef: { kind: 'exact', datasetId: 'dataset-exact', revisionId: 'revision-3' },
    }, 'Selected dataset · Exact version revision-3'],
    ['as-of', {
      uri: 'file:///data/as-of.csv',
      datasetRef: {
        kind: 'as_of', asOf: '2026-07-24T00:00:00Z',
        resolved: {
          datasetId: 'dataset-as-of', revisionId: 'revision-4',
          committedAt: '2026-07-23T23:00:00Z', retentionOwner: 'provider', selector: 'as_of',
        },
      },
    }, 'Selected dataset · Exact version revision-4'],
    ['run-time parameter', {
      uri: 'file:///data/runtime.csv',
      datasetRef: { parameterRef: 'runtime_dataset' },
    }, 'Run-time dataset parameter'],
  ])('recognizes a legal %s dataset binding without transient catalog hints', (_case, config, summary) => {
    selectSource(config)
    render(<Inspector />)
    expect(screen.getByTitle(summary)).toBeInTheDocument()
    expect(screen.queryByLabelText('Dataset URI')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('CSV delimiter')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('CSV header row')).not.toBeInTheDocument()
  })

  it.each([
    ['tableId only', { tableId: 'events', uri: 'file:///data/manual-input.csv' }],
    ['stale transient hints', {
      tableId: 'events', providerResourceRef: 'dataset:stale-placement', providerMountId: 'stale-mount',
      uri: 'file:///data/manual-input.csv',
    }],
    ['providerResourceRef only', {
      providerResourceRef: 'dataset:display-placement',
      uri: 'file:///data/manual-input.csv',
    }],
  ])('does not treat %s as a bound Source identity', (_case, config) => {
    const table: CatalogTable = {
      id: 'events', registrationId: 'dataset:events', name: 'events',
      uri: 'file:///workspace/events.parquet', rowCount: 2000, columns: cols,
    }
    selectSource(config, [table])
    render(<Inspector />)
    expect(screen.getByTitle('Manual URI · Delimited text')).toBeInTheDocument()
    expect(screen.getByText('dataset uri')).toBeInTheDocument()
    expect(screen.getByText(/CSV delimiter/)).toBeInTheDocument()
    expect(screen.getByText('CSV header row')).toBeInTheDocument()
    expect(screen.queryByTitle(/Current version/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Connection details'))
    const details = screen.getByLabelText('Source connection details')
    expect(details).toHaveTextContent('Manual URI')
    expect(details).not.toHaveTextContent('Catalog registration')
    expect(details).not.toHaveTextContent('Provider resource')
  })

  it('keeps URI and CSV parsing controls for a manual CSV Source', () => {
    selectSource({ uri: 'file:///data/manual-input.csv', delimiter: ';', header: 'yes' })
    render(<Inspector />)
    expect(screen.getByTitle('Manual URI · Delimited text')).toBeInTheDocument()
    expect(screen.getByText('Properties')).toBeInTheDocument()
    expect(screen.getByText('dataset uri')).toBeInTheDocument()
    expect(screen.getByText(/CSV delimiter/)).toBeInTheDocument()
    expect(screen.getByText('CSV header row')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Connection details'))
    expect(screen.getByLabelText('Source connection details')).toHaveTextContent('Manual URI')
  })

  it('keeps a changed Workspace Source bound, then returns a cleared Source to draft entry', () => {
    const table: CatalogTable = {
      id: 'events', registrationId: 'dataset:events', name: 'events',
      uri: 'file:///workspace/events.parquet', rowCount: 2000, columns: cols,
    }
    selectSource({
      uri: table.uri, tableId: table.id, registrationId: table.registrationId,
    }, [table])
    render(<Inspector />)
    expect(screen.getByTitle(/Local catalog · Current version/)).toBeInTheDocument()

    act(() => {
      useStore.setState((state) => ({
        doc: {
          ...state.doc,
          nodes: state.doc.nodes.map((node) => (
            node.id === 'source'
              ? { ...node, data: { ...node.data, config: {
                uri: 'provider+dataset://mount/source-binding',
                providerResourceRef: 'dataset:external/orders',
                providerMountId: 'mount',
                providerSourceBindingId: 'source-binding',
                providerName: 'Luma Data API',
                providerReadMode: 'exact',
                datasetRef: {
                  kind: 'exact',
                  datasetId: 'provider-dataset-identity',
                  revisionId: 'provider-revision-7',
                },
              } } }
              : node
          )),
        },
      }))
    })
    expect(screen.getByTitle('Luma Data API · Exact version provider-revision-7')).toBeInTheDocument()
    expect(screen.queryByText('Choose data')).not.toBeInTheDocument()

    act(() => {
      useStore.setState((state) => ({
        doc: {
          ...state.doc,
          nodes: state.doc.nodes.map((node) => (
            node.id === 'source'
              ? { ...node, data: { ...node.data, config: {} } }
              : node
          )),
        },
      }))
    })
    expect(screen.getByTitle('Choose a registered dataset')).toBeInTheDocument()
    expect(screen.getByText('Choose data')).toBeInTheDocument()
  })
})

describe('Inspector — Join configuration', () => {
  const selectJoin = (config: Record<string, unknown>) => {
    registerGenericNodes([{
      kind: 'join', title: 'join', category: 'compute', tag: 'join',
      inputs: [
        { id: 'a', label: 'left', wire: 'dataset' },
        { id: 'b', label: 'right', wire: 'dataset' },
      ],
      outputs: [{ id: 'out', wire: 'dataset' }],
      params: [
        { name: 'how', type: 'select', label: 'how', default: 'inner', options: ['inner', 'left', 'right', 'outer'] },
        { name: 'on', type: 'string', label: 'shared key(s)' },
        { name: 'condition', type: 'string', label: 'or ON expression (a.x = b.y)' },
      ],
      canBypass: false, previewable: true, blurb: 'Combine two datasets by matching rows',
    }])
    useStore.setState({
      selectedIds: ['join'], canvasRole: 'owner', runs: {},
      doc: {
        id: 'join-inspector', name: 'Join Inspector', version: 1, requirements: [],
        nodes: [{
          id: 'join', type: 'join', position: { x: 100, y: 100 },
          data: { title: 'join', status: 'draft', history: [], config },
        }],
        edges: [],
      },
      schemas: { join: { out: cols } },
      nodeRevealRequest: null,
    } as any)
  }

  it('summarizes different-name and multi-column keys without a second generic editor', () => {
    selectJoin({
      how: 'left',
      on: '',
      condition: 'a._rowid = b.original_row_id AND a.region = b.region_id',
    })
    render(<Inspector />)

    expect(screen.getByText('Join configuration')).toBeVisible()
    expect(screen.getByText('a._rowid = b.original_row_id')).toBeVisible()
    expect(screen.getByText('a.region = b.region_id')).toBeVisible()
    expect(screen.queryByText('shared key(s)')).not.toBeInTheDocument()
    expect(screen.queryByText(/ON expression/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Edit keys on Join card' }))
    expect(useStore.getState().nodeRevealRequest).toMatchObject({
      canvasId: 'join-inspector',
      nodeId: 'join',
    })
  })

  it('preserves an unrepresentable condition as a read-only advanced summary', () => {
    const condition = 'a.id = b.id OR a.region = b.region_id'
    selectJoin({ how: 'inner', on: 'obsolete', condition })
    render(<Inspector />)

    expect(screen.getByText('Advanced condition')).toBeVisible()
    expect(screen.getByText(condition)).toBeVisible()
    expect(useStore.getState().doc.nodes[0].data.config).toMatchObject({
      on: 'obsolete',
      condition,
    })
  })
})

describe('Inspector — Join hints', () => {
  const edge = (id: string, source: string, targetHandle: string): CanvasEdge => ({
    id, source, sourceHandle: 'out', target: 'join', targetHandle, data: { wire: 'dataset' },
  })
  const leftEdge = edge('left-join', 'left', 'a')
  const rightEdge = edge('right-join', 'right', 'b')

  const selectJoin = (edges: CanvasEdge[]) => {
    useStore.setState({
      selectedIds: ['join'], canvasRole: 'owner', runs: {},
      doc: {
        id: 'join-hints', name: 'Join hints', version: 1, requirements: [],
        nodes: [
          { id: 'left', type: 'source', position: { x: 0, y: 0 }, data: { title: 'left', status: 'draft', history: [], config: { uri: 'left.parquet' } } },
          { id: 'right', type: 'source', position: { x: 0, y: 1 }, data: { title: 'right', status: 'draft', history: [], config: { uri: 'right.parquet' } } },
          { id: 'join', type: 'join', position: { x: 1, y: 0 }, data: { title: 'join', status: 'draft', history: [], config: { on: 'id' } } },
        ],
        edges,
      },
      schemas: { left: { out: cols }, right: { out: cols }, join: { out: cols } },
      catalog: [],
      previews: {},
    } as any)
  }

  const advanceAnalysis = async () => {
    await act(async () => { await vi.advanceTimersByTimeAsync(350) })
  }

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it.each([
    ['zero inputs', []],
    ['only the left input', [leftEdge]],
    ['only the right input', [rightEdge]],
    ['two edges on the left input', [leftEdge, edge('other-left-join', 'right', 'a')]],
  ])('does not request or render Join hints with %s', async (_case, edges) => {
    vi.useFakeTimers()
    const joinAnalysis = vi.spyOn(api, 'joinAnalysis').mockResolvedValue({ suggestions: [] } as any)
    vi.spyOn(api, 'plan').mockResolvedValue({ regions: [] } as any)
    selectJoin(edges)

    render(<Inspector />)
    await advanceAnalysis()

    expect(joinAnalysis).not.toHaveBeenCalled()
    expect(screen.queryByText('Join hints')).not.toBeInTheDocument()
    expect(screen.queryByText('No matching key columns between the two inputs.')).not.toBeInTheDocument()
  })

  it('runs analysis for one left and one right semantic input', async () => {
    vi.useFakeTimers()
    const joinAnalysis = vi.spyOn(api, 'joinAnalysis').mockResolvedValue({
      suggestions: [{
        leftColumns: ['id'], rightColumns: ['id'], cardinality: '1:1',
        confidence: 'verified', score: 2, reason: 'matching key',
      }],
      warning: null,
      note: null,
    } as any)
    vi.spyOn(api, 'plan').mockResolvedValue({ regions: [] } as any)
    selectJoin([rightEdge, leftEdge])

    render(<Inspector />)
    expect(screen.getByText('Analyzing keys…')).toBeInTheDocument()
    await advanceAnalysis()

    expect(joinAnalysis).toHaveBeenCalledTimes(1)
    expect(screen.getByText('id = id')).toBeInTheDocument()
    expect(screen.queryByText('No matching key columns between the two inputs.')).not.toBeInTheDocument()
  })

  it('keeps the genuine no-match fallback for a complete analysis without suggestions', async () => {
    vi.useFakeTimers()
    const joinAnalysis = vi.spyOn(api, 'joinAnalysis').mockResolvedValue({
      suggestions: [], warning: null, note: null,
    } as any)
    vi.spyOn(api, 'plan').mockResolvedValue({ regions: [] } as any)
    selectJoin([leftEdge, rightEdge])

    render(<Inspector />)
    await advanceAnalysis()

    expect(joinAnalysis).toHaveBeenCalledTimes(1)
    expect(screen.getByText('No matching key columns between the two inputs.')).toBeInTheDocument()
  })

  it('does not describe a failed complete analysis as no matching keys', async () => {
    vi.useFakeTimers()
    const joinAnalysis = vi.spyOn(api, 'joinAnalysis').mockRejectedValue(new Error('analysis unavailable'))
    vi.spyOn(api, 'plan').mockResolvedValue({ regions: [] } as any)
    selectJoin([leftEdge, rightEdge])

    render(<Inspector />)
    await advanceAnalysis()

    expect(joinAnalysis).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Key suggestions are unavailable. Choose join keys manually.')).toBeInTheDocument()
    expect(screen.queryByText('No matching key columns between the two inputs.')).not.toBeInTheDocument()
  })

  it('renders a configured blocking code and never describes unknown evidence as safe', async () => {
    const joinAnalysis = vi.spyOn(api, 'joinAnalysis').mockResolvedValue({
      suggestions: [
        { leftColumns: ['account_id'], rightColumns: ['id'], cardinality: '1:1', confidence: 'verified', score: 4, reason: 'reference', rowReference: [{ leftField: 'account_id', rightField: 'id', status: 'compatible', reason: 'exact_target_matches_peer' }] },
        { leftColumns: ['legacy_id'], rightColumns: ['id'], cardinality: 'unknown', confidence: 'inferred', score: 1, reason: 'unknown', rowReference: [{ leftField: 'legacy_id', rightField: 'id', status: 'unknown', reason: 'peer_identity_unavailable' }] },
      ], warning: null, note: null, configuredRowReference: [], blockingCode: 'row_reference_target_mismatch',
    } as any)
    useStore.setState({
      selectedIds: ['join'], canvasRole: 'owner', runs: {},
      doc: { id: 'join-diagnosis', name: 'Join', version: 1, requirements: [],
        nodes: [
          { id: 'left', type: 'source', position: { x: 0, y: 0 }, data: { title: 'left', status: 'draft', history: [], config: { uri: 'left.parquet' } } },
          { id: 'right', type: 'source', position: { x: 0, y: 1 }, data: { title: 'right', status: 'draft', history: [], config: { uri: 'right.parquet' } } },
          { id: 'join', type: 'join', position: { x: 1, y: 0 }, data: { title: 'join', status: 'draft', history: [], config: { on: 'account_id' } } },
        ], edges: [
          leftEdge,
          rightEdge,
        ] },
      schemas: { left: { out: cols }, right: { out: cols }, join: { out: cols } },
    } as any)
    render(<Inspector />)
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('row_reference_target_mismatch'))
    expect(screen.getByText('reference match')).toBeInTheDocument()
    expect(screen.getByText('reference unknown')).toBeInTheDocument()
    expect(screen.queryByText(/reference safe/i)).not.toBeInTheDocument()
  })
})

describe('Inspector — execution-plan hierarchy', () => {
  const selectTransform = () => {
    useStore.setState({
      selectedIds: ['transform'],
      canvasRole: 'owner',
      runs: {},
      doc: {
        id: 'execution-plan', name: 'Execution plan', version: 1, requirements: [], edges: [],
        nodes: [{
          id: 'transform', type: 'transform', position: { x: 0, y: 0 },
          data: {
            title: 'transform', status: 'draft', history: [],
            config: { mode: 'map', code: 'def fn(row):\n    return row' },
          },
        }],
      },
      schemas: { transform: { out: cols } },
    } as any)
  }

  afterEach(() => vi.restoreAllMocks())

  it('keeps scheduler identities behind one Run plan disclosure', async () => {
    const plan = vi.spyOn(api, 'plan').mockResolvedValue({
      regions: [
        { id: 'source-internal', outputNode: 'source-9342868352a9', backend: 'default', tier: 'object', rows: 2_000, confidence: 'exact' },
        { id: 'join-internal', outputNode: 'join-5-33741', backend: 'ray-data', tier: null, rows: 500, confidence: 'bounded', requires: '8GB' },
      ],
    } as any)
    selectTransform()
    render(<Inspector />)

    await waitFor(() => expect(plan).toHaveBeenCalled())
    expect(screen.getByTestId('run-plan-summary')).toHaveTextContent('2 execution groups · local + Ray')
    expect(screen.getByTestId('run-plan-summary')).not.toHaveTextContent('source-9342868352a9')
    const details = screen.getByTestId('run-plan-details')
    expect(details).not.toHaveAttribute('open')
    expect(screen.getByText('source-9342868352a9')).not.toBeVisible()
    fireEvent.click(screen.getByText('Run plan'))
    expect(details).toHaveAttribute('open')
    expect(screen.getByText('source-9342868352a9')).toBeVisible()
    expect(screen.getByText('join-5-33741')).toBeVisible()
    expect(screen.getByText('ray-data')).toBeVisible()
    expect(screen.getByTitle('materialization tier for the handoff')).toHaveTextContent('object')
  })

  it('does not present parallel branches as a serial backend path', async () => {
    const plan = vi.spyOn(api, 'plan').mockResolvedValue({
      regions: [
        { id: 'left', outputNode: 'left-branch', backend: 'default', tier: 'object', rows: 100, confidence: 'exact' },
        { id: 'right', outputNode: 'right-branch', backend: 'ray-data', tier: 'object', rows: 100, confidence: 'exact' },
        { id: 'join', outputNode: 'join-output', backend: 'default', tier: null, rows: 100, confidence: 'bounded' },
      ],
    } as any)
    selectTransform()
    render(<Inspector />)

    await waitFor(() => expect(plan).toHaveBeenCalled())
    const summary = screen.getByTestId('run-plan-summary')
    expect(summary).toHaveTextContent('3 execution groups · local + Ray')
    expect(summary).not.toHaveTextContent('→')
    expect(summary).not.toHaveTextContent('local + Ray + local')
  })

  it('keeps unsatisfied resources and source preflight warnings visible', async () => {
    const plan = vi.spyOn(api, 'plan').mockResolvedValue({
      regions: [{
        id: 'blocked-internal', outputNode: 'transform-generated-id', backend: 'default',
        tier: null, rows: null, confidence: 'unknown', requires: '8GB', unsatisfied: true,
        available: 'local has 4GB', preflight: ['Pinned source revision is unavailable.'],
      }],
    } as any)
    selectTransform()
    render(<Inspector />)

    await waitFor(() => expect(plan).toHaveBeenCalled())
    const alerts = screen.getAllByRole('alert')
    expect(alerts.some((alert) => alert.textContent?.includes('Needs 8GB — local has 4GB.'))).toBe(true)
    expect(alerts.some((alert) => alert.textContent?.includes('Pinned source revision is unavailable.'))).toBe(true)
    expect(screen.getByText('transform-generated-id')).not.toBeVisible()
  })

  it('still hides a trivial local plan', async () => {
    const plan = vi.spyOn(api, 'plan').mockResolvedValue({
      regions: [{
        id: 'local-only', outputNode: 'transform', backend: 'default',
        tier: null, rows: 10, confidence: 'exact',
      }],
    } as any)
    selectTransform()
    render(<Inspector />)

    await waitFor(() => expect(plan).toHaveBeenCalled())
    expect(screen.queryByText('Execution path')).not.toBeInTheDocument()
    expect(screen.queryByText('Run plan')).not.toBeInTheDocument()
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
