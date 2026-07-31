import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ReactFlowProvider } from '@xyflow/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'

const apiMocks = vi.hoisted(() => ({
  installedProcessorSource: vi.fn(),
}))
vi.mock('../../api/client', () => ({
  api: new Proxy(apiMocks, {
    get: (target, property) => (
      property in target
        ? target[property as keyof typeof target]
        : async () => ({})
    ),
  }),
  KernelError: class KernelError extends Error {},
  setApiUser: vi.fn(),
}))
vi.mock('../../ui/CodeEditor', () => ({
  CodeEditor: () => <div data-testid="code-editor" />,
}))
vi.mock('../../ui/CodeSnippet', () => ({
  CodeSnippet: () => <div data-testid="code-snippet" />,
}))
vi.mock('../../panels/DataPanel', () => ({
  DataPanel: ({ editorPreview }: {
    editorPreview?: { onRunUpstream?: () => void; testTarget?: string }
  }) => (
    <>
      {editorPreview?.testTarget && <span>test target: {editorPreview.testTarget}</span>}
      {editorPreview?.onRunUpstream
        ? <button type="button" onClick={editorPreview.onRunUpstream}>Run upstream</button>
        : null}
    </>
  ),
}))

import './transform'
import { getComponent } from '../registry'
import { previewPlanIdentity, useStore } from '../../store/graph'
import { CodeFullscreen } from '../../panels/CodeFullscreen'

const PROCESSOR_ID = `tr_${'a'.repeat(29)}`
const node = {
  id: 'transform', type: 'transform', position: { x: 0, y: 0 },
  data: { title: 'transform', status: 'draft' as const, config: {
    source: 'library', processor: PROCESSOR_ID, version: 'v1', mode: 'map', code: null,
  } },
}

describe('Transform exact processor labels', () => {
  beforeEach(() => {
    apiMocks.installedProcessorSource.mockReset()
      .mockRejectedValue(Object.assign(new Error('source unavailable'), { status: 404 }))
    useStore.setState({
      canvasRole: 'owner', fullscreenCode: null, previews: {},
      doc: { id: 'canvas', name: 'canvas', version: 1, requirements: [], nodes: [node], edges: [] },
      canvasTransformReferences: [],
      processors: [{
        id: PROCESSOR_ID, version: 'v2', title: 'Latest version', mode: 'map',
        category: 'compute', inputColumns: [], inputSchema: [], outputSchema: [], requirements: [],
        paramsSchema: {}, previewable: true, blurb: '', provenance: 'promoted',
      }],
    } as any)
  })

  it('does not label a pinned old version as the listed latest descriptor', () => {
    const Transform = getComponent('transform')!
    render(
      <TooltipProvider><ReactFlowProvider>
        <Transform id={node.id} data={node.data} />
      </ReactFlowProvider></TooltipProvider>,
    )

    expect(screen.getAllByText(`${PROCESSOR_ID}@v1`).length).toBeGreaterThan(0)
    expect(screen.queryByText('Latest version')).not.toBeInTheDocument()
    expect(screen.queryByText('select processor')).not.toBeInTheDocument()
  })

  it('opens a visible exact Canvas/node upgrade context only from Manage', () => {
    const Transform = getComponent('transform')!
    render(
      <TooltipProvider><ReactFlowProvider>
        <Transform id={node.id} data={node.data} />
      </ReactFlowProvider></TooltipProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: /Manage/ }))
    expect(useStore.getState()).toMatchObject({
      view: 'transforms', transformResourceId: PROCESSOR_ID, transformVersion: 'v1',
      transformUpgradeCanvasId: 'canvas', transformUpgradeNodeId: 'transform',
    })
  })

  it('opens a Library definition instead of advertising editable source code', () => {
    const Transform = getComponent('transform')!
    useStore.setState({ selectedIds: [node.id] })
    render(
      <TooltipProvider><ReactFlowProvider>
        <Transform id={node.id} data={node.data} />
      </ReactFlowProvider></TooltipProvider>,
    )

    expect(screen.getByRole('button', { name: 'View processor definition' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Edit code' })).not.toBeInTheDocument()
  })

  it('shows an unlisted shared exact ref in the fullscreen read-only label', async () => {
    useStore.setState({
      canvasRole: 'viewer', processors: [],
      fullscreenCode: { nodeId: node.id, param: 'code', lang: 'python' },
    } as any)

    render(<CodeFullscreen />)

    expect((await screen.findAllByText(new RegExp(`${PROCESSOR_ID}@v1`))).length).toBeGreaterThan(0)
    expect(screen.queryByText(/Latest version/)).not.toBeInTheDocument()
    await screen.findByText(/bounded testing cannot be enabled safely/i)
    expect(screen.queryByText(/Use Test transform to run/i)).not.toBeInTheDocument()
  })

  it('uses the Canvas-authorized exact descriptor for a shared Canvas viewer', async () => {
    const source = {
      id: 'source', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'Shared source', status: 'latest' as const, config: { uri: 'shared.parquet' } },
    }
    const descriptor = {
      id: PROCESSOR_ID, version: 'v1', title: 'Owner promoted processor', mode: 'map',
      category: 'compute', inputColumns: [], inputSchema: [],
      outputSchema: [{ name: 'normalized', type: 'string', nullable: false, capabilities: [] }],
      requirements: [], paramsSchema: {}, previewable: true,
      blurb: 'Normalizes rows for the shared research workflow.',
      provenance: 'promoted' as const,
    }
    const sharedDoc = {
      id: 'shared-canvas', name: 'Shared', version: 1, requirements: [],
      nodes: [source, node],
      edges: [{
        id: 'source-transform', source: 'source', sourceHandle: 'out',
        target: 'transform', targetHandle: 'in', data: { wire: 'dataset' as const },
      }],
    }
    useStore.setState({
      canvasRole: 'viewer',
      doc: sharedDoc,
      processors: [],
      canvasTransformReferences: [{
        id: PROCESSOR_ID,
        version: 'v1',
        availability: 'available',
        descriptor,
      }],
      fullscreenCode: { nodeId: node.id, param: 'code', lang: 'python' },
      editorPreviews: { transform: {
        canvasId: sharedDoc.id,
        nodeId: 'transform',
        planIdentity: previewPlanIdentity(sharedDoc, 'transform'),
        parameterBindings: [],
        requestGeneration: 1,
        offset: 0,
        result: {
          columns: descriptor.outputSchema,
          rows: [{ normalized: 'ready' }],
          truncated: false,
          editorTestInput: {
            runId: 'shared-source-run', nodeId: 'source', portId: 'out',
            label: 'Shared source', rows: 1,
          },
        },
      } },
    } as any)

    render(<CodeFullscreen />)

    const definition = await screen.findByRole('region', { name: 'Library processor definition' })
    expect(definition).toHaveTextContent('Owner promoted processor')
    expect(definition).toHaveTextContent('Normalizes rows for the shared research workflow.')
    expect(screen.getByRole('button', { name: 'Test transform' })).toBeEnabled()
    expect(screen.queryByText(/bounded testing cannot be enabled safely/i)).not.toBeInTheDocument()
  })

  it('shows the owner-scoped exact source for a promoted Library processor', async () => {
    const promotedSource = [
      'def fn(row):',
      "    return {**row, 'normalized': True}",
    ].join('\n')
    apiMocks.installedProcessorSource.mockResolvedValue({
      processorId: PROCESSOR_ID,
      version: 'v1',
      language: 'python',
      source: promotedSource,
      sha256: 'b'.repeat(64),
    })
    useStore.setState({
      processors: [{
        id: PROCESSOR_ID, version: 'v1', title: 'Normalize rows', mode: 'map',
        category: 'compute', inputColumns: [], inputSchema: [], outputSchema: [], requirements: [],
        paramsSchema: {}, previewable: true, blurb: 'Owner-promoted row normalizer.',
        provenance: 'promoted',
      }],
      fullscreenCode: { nodeId: node.id, param: 'code', lang: 'python' },
    } as any)

    render(<CodeFullscreen />)

    const implementation = await screen.findByRole('region', { name: 'Installed processor source' })
    expect(apiMocks.installedProcessorSource).toHaveBeenCalledWith(PROCESSOR_ID, 'v1')
    expect(implementation).toHaveTextContent("return {**row, 'normalized': True}")
    expect(implementation).toHaveTextContent(`SHA-256 ${'b'.repeat(64)}`)
    const details = screen.getByText('Technical details').closest('details')
    expect(details).not.toHaveAttribute('open')
    expect(within(details as HTMLElement).getByText(`${PROCESSOR_ID}@v1`)).not.toBeVisible()
  })

  it('shows the exact Library definition and tests a previewable processor on retained upstream rows', async () => {
    const installedSource = [
      'MAX_DECODED_IMAGE_PIXELS = 50_000_000',
      '',
      'def processor_factory(params):',
      '    return lambda row: row',
      '',
    ].join('\n')
    apiMocks.installedProcessorSource.mockResolvedValue({
      processorId: PROCESSOR_ID,
      version: 'v1',
      language: 'python',
      source: installedSource,
      sha256: 'a'.repeat(64),
    })
    const source = {
      id: 'source', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'Image source', status: 'latest' as const, config: { uri: 'images.parquet' } },
    }
    const descriptor = {
      id: PROCESSOR_ID, version: 'v1', title: 'Add Height/Width', mode: 'map',
      category: 'compute', inputColumns: ['_rowid'],
      inputSchema: [{ name: '_rowid', type: 'uint64', nullable: false, capabilities: [] }],
      outputSchema: [
        { name: 'height', type: 'int32', nullable: true, capabilities: [] },
        { name: 'width', type: 'int32', nullable: true, capabilities: [] },
      ],
      requirements: [], paramsSchema: {
        image_key: { type: 'string', default: 'image' },
        max_decoded_image_pixels: { type: 'integer', default: 50_000_000 },
      },
      previewable: true,
      blurb: 'Adds decoded image height and width while preserving row identity.',
      provenance: 'plugin' as const,
    }
    const doc = {
      id: 'canvas', name: 'canvas', version: 1, requirements: [],
      nodes: [source, node],
      edges: [{
        id: 'source-transform', source: 'source', sourceHandle: 'out',
        target: 'transform', targetHandle: 'in', data: { wire: 'dataset' as const },
      }],
    }
    const runEditorPreview = vi.fn()
    useStore.setState({
      doc,
      processors: [descriptor],
      kernelUp: true,
      fullscreenCode: { nodeId: node.id, param: 'code', lang: 'python' },
      runEditorPreview,
      editorPreviews: { transform: {
        canvasId: doc.id,
        nodeId: 'transform',
        planIdentity: previewPlanIdentity(doc, 'transform'),
        parameterBindings: [],
        requestGeneration: 1,
        offset: 0,
        result: {
          columns: descriptor.outputSchema,
          rows: [{ height: 16, width: 32 }],
          truncated: false,
          editorTestInput: {
            runId: 'source-run', nodeId: 'source', portId: 'out',
            label: 'Image source', rows: 1,
          },
        },
      } },
    } as any)

    render(<CodeFullscreen />)

    const definition = await screen.findByRole('region', { name: 'Library processor definition' })
    expect(definition).toHaveTextContent(`${PROCESSOR_ID}@v1`)
    expect(definition).toHaveTextContent('Adds decoded image height and width while preserving row identity.')
    expect(definition).toHaveTextContent('Plugin')
    expect(definition).toHaveTextContent('Bounded testSupported')
    expect(definition).toHaveTextContent('_rowid')
    expect(definition).toHaveTextContent('height')
    expect(definition).toHaveTextContent('width')
    expect(definition).toHaveTextContent('image_key')
    expect(definition).toHaveTextContent('default "image"')
    const implementation = await screen.findByRole('region', { name: 'Installed processor source' })
    expect(apiMocks.installedProcessorSource).toHaveBeenCalledWith(PROCESSOR_ID, 'v1')
    expect(implementation).toHaveTextContent('Installed processor source')
    expect(implementation).toHaveTextContent('exact local implementation')
    expect(implementation).toHaveTextContent('does not indicate remote or distributed dispatch')
    expect(implementation).toHaveTextContent('MAX_DECODED_IMAGE_PIXELS = 50_000_000')
    expect(implementation).toHaveTextContent(`SHA-256 ${'a'.repeat(64)}`)
    expect(definition).not.toHaveTextContent('Implementation source unavailable')
    expect(screen.queryByTestId('code-editor')).not.toBeInTheDocument()

    const test = screen.getByRole('button', { name: 'Test transform' })
    expect(test).toBeEnabled()
    expect(screen.getByText('test target: transform')).toBeInTheDocument()
    fireEvent.click(test)
    expect(runEditorPreview).toHaveBeenCalledWith('transform')
    expect(screen.queryByRole('button', { name: 'Example rows' })).not.toBeInTheDocument()
  })

  it('keeps an honest unavailable state when a plugin does not publish source', async () => {
    const sourceNode = {
      id: 'source', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'Source', status: 'latest' as const, config: { uri: 'rows.parquet' } },
    }
    const connectedDoc = {
      id: 'canvas', name: 'canvas', version: 1, requirements: [],
      nodes: [sourceNode, node],
      edges: [{
        id: 'source-transform', source: 'source', sourceHandle: 'out',
        target: 'transform', targetHandle: 'in', data: { wire: 'dataset' as const },
      }],
    }
    useStore.setState({
      doc: connectedDoc,
      processors: [{
        id: PROCESSOR_ID, version: 'v1', title: 'Private transform', mode: 'map',
        category: 'compute', inputColumns: [], inputSchema: [], outputSchema: [], requirements: [],
        paramsSchema: {}, previewable: true, blurb: 'A processor without published source.',
        provenance: 'plugin',
      }],
      editorPreviews: { transform: {
        canvasId: connectedDoc.id,
        nodeId: 'transform',
        planIdentity: previewPlanIdentity(connectedDoc, 'transform'),
        parameterBindings: [],
        requestGeneration: 1,
        offset: 0,
        result: {
          columns: [], rows: [{ id: 1 }], truncated: false,
          editorTestInput: {
            runId: 'source-run', nodeId: 'source', portId: 'out',
            label: 'Source', rows: 1,
          },
        },
      } },
      fullscreenCode: { nodeId: node.id, param: 'code', lang: 'python' },
    } as any)

    render(<CodeFullscreen />)

    expect(await screen.findByText('Implementation source unavailable')).toBeVisible()
    expect(apiMocks.installedProcessorSource).toHaveBeenCalledWith(PROCESSOR_ID, 'v1')
    expect(screen.queryByRole('region', { name: 'Installed processor source' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Test transform' })).toBeEnabled()
  })

  it('explains why a non-previewable Library processor cannot be tested', async () => {
    useStore.setState({
      processors: [{
        id: PROCESSOR_ID, version: 'v1', title: 'Whole table optimizer', mode: 'map_batches',
        category: 'compute', inputColumns: [], inputSchema: [], outputSchema: [], requirements: [],
        paramsSchema: {}, previewable: false, blurb: 'Optimizes a complete table.',
        provenance: 'plugin',
      }],
      fullscreenCode: { nodeId: node.id, param: 'code', lang: 'python' },
    } as any)

    render(<CodeFullscreen />)

    expect(await screen.findByText(/does not support bounded preview tests/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Test transform' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('code-editor')).not.toBeInTheDocument()
  })

  it('requires and preserves a description when promoting an ad-hoc Transform', async () => {
    const adhocNode = {
      ...node,
      data: { ...node.data, config: {
        source: 'adhoc', mode: 'map', code: 'def fn(row): return row',
      } },
    }
    const promote = vi.fn().mockResolvedValue(undefined)
    useStore.setState({
      doc: {
        id: 'canvas', name: 'canvas', version: 1, requirements: [],
        nodes: [adhocNode], edges: [],
      },
      fullscreenCode: { nodeId: adhocNode.id, param: 'code', lang: 'python' },
      promote,
    } as any)

    render(<CodeFullscreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Promote to library' }))
    const dialog = screen.getByRole('dialog', { name: /Promote transform to the Library/i })
    const submit = within(dialog).getByRole('button', { name: 'Promote' })
    expect(submit).toBeDisabled()
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Description' }), {
      target: { value: '  Normalizes each row for downstream training.  ' },
    })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)

    await waitFor(() => expect(promote).toHaveBeenCalledWith(
      'transform', 'Normalizes each row for downstream training.',
    ))
  })

  it('labels ad-hoc transforms with their actual operator semantics', () => {
    const adhocNode = {
      ...node,
      data: { ...node.data, config: { source: 'adhoc', mode: 'map', code: 'def fn(row): return row' } },
    }
    useStore.setState({
      doc: { id: 'canvas', name: 'canvas', version: 1, requirements: [], nodes: [adhocNode], edges: [] },
    } as any)
    const Transform = getComponent('transform')!
    render(
      <TooltipProvider><ReactFlowProvider>
        <Transform id={adhocNode.id} data={adhocNode.data} />
      </ReactFlowProvider></TooltipProvider>,
    )

    expect(screen.getByText('map · Python')).toBeInTheDocument()
    expect(screen.queryByText(/runs over/i)).not.toBeInTheDocument()
  })

  it('does not expose a run-scope control in the fullscreen ad-hoc editor', async () => {
    const adhocNode = {
      ...node,
      data: { ...node.data, config: { source: 'adhoc', mode: 'map', code: 'def fn(row): return row' } },
    }
    useStore.setState({
      doc: { id: 'canvas', name: 'canvas', version: 1, requirements: [], nodes: [adhocNode], edges: [] },
      fullscreenCode: { nodeId: adhocNode.id, param: 'code', lang: 'python' },
    } as any)

    render(<CodeFullscreen />)

    expect(await screen.findByTestId('code-editor')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: /runs over/i })).not.toBeInTheDocument()
  })

  it('keeps Example rows local to one fullscreen editor session', async () => {
    const adhocNode = {
      ...node,
      data: { ...node.data, config: {
        source: 'adhoc', mode: 'map', code: 'def fn(row): return row',
      } },
    }
    const doc = {
      id: 'canvas', name: 'canvas', version: 1, requirements: [],
      nodes: [adhocNode], edges: [],
    }
    const runEditorExamplePreview = vi.fn()
    useStore.setState({
      doc,
      kernelUp: true,
      fullscreenCode: { nodeId: adhocNode.id, param: 'code', lang: 'python' },
      runEditorExamplePreview,
    } as any)

    render(<CodeFullscreen />)

    expect(await screen.findByRole('button', { name: 'Upstream result' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Example rows' }))
    expect(screen.getByText('Test only')).toBeInTheDocument()
    expect(screen.getByText('These rows are used only for this test and are not saved to the Canvas. Edit them, then choose Test code.')).toBeInTheDocument()
    const fixture = screen.getByRole('textbox', { name: 'Example rows JSON' })
    expect(fixture).toHaveValue('[\n  {\n    "value": 1\n  }\n]')

    fireEvent.change(fixture, { target: { value: '[1]' } })
    expect(screen.getByRole('alert')).toHaveTextContent('Every example row must be a JSON object')
    expect(screen.getByRole('button', { name: 'Test code' })).toBeDisabled()
    expect(useStore.getState().doc).toBe(doc)

    fireEvent.change(fixture, { target: { value: '[{"value":9}]' } })
    fireEvent.click(screen.getByRole('button', { name: 'Test code' }))
    expect(runEditorExamplePreview).toHaveBeenCalledWith(
      'transform', '[{"value":9}]', 0, undefined,
    )
    expect(useStore.getState().doc).toBe(doc)

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    useStore.getState().openCodeFullscreen('transform', 'code', 'python')
    await waitFor(() => expect(
      screen.getByRole('button', { name: 'Upstream result' }),
    ).toHaveAttribute('aria-pressed', 'true'))
    fireEvent.click(screen.getByRole('button', { name: 'Example rows' }))
    expect(screen.getByRole('textbox', { name: 'Example rows JSON' }))
      .toHaveValue('[\n  {\n    "value": 1\n  }\n]')
  })

  it('starts an Example rows test with numeric values from a known Join schema', async () => {
    const join = {
      id: 'join', type: 'join', position: { x: 0, y: 0 },
      data: { title: 'join', status: 'latest' as const, config: {} },
    }
    const adhocNode = {
      ...node,
      data: { ...node.data, config: {
        source: 'adhoc', mode: 'map', code: 'def fn(row):\n  row["id_plus_one"] = row["id"] + 1\n  return row',
      } },
    }
    useStore.setState({
      doc: {
        id: 'canvas', name: 'canvas', version: 1, requirements: [], nodes: [join, adhocNode],
        edges: [{ id: 'join-transform', source: 'join', sourceHandle: 'out', target: 'transform', data: { wire: 'dataset' } }],
      },
      schemas: { join: { out: [
        { name: 'id', type: 'bigint', capabilities: [] },
        { name: 'user_id', type: 'int64', capabilities: [] },
      ] } },
      fullscreenCode: { nodeId: 'transform', param: 'code', lang: 'python' },
    } as any)

    render(<CodeFullscreen />)

    fireEvent.click(await screen.findByRole('button', { name: 'Example rows' }))
    const fixture = screen.getByRole('textbox', { name: 'Example rows JSON' })
    const row = JSON.parse((fixture as HTMLTextAreaElement).value)[0]
    expect(row).toEqual({ id: 1, user_id: 1 })
    expect(row.id + 1).toBe(2)
  })

  it('keeps upstream run confirmation, progress, success, and failure in the fullscreen editor', async () => {
    const source = {
      id: 'source', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'Source input', status: 'draft' as const, config: { uri: 'events.parquet' } },
    }
    const upstream = {
      id: 'sample', type: 'sample', position: { x: 0, y: 0 },
      data: { title: 'Sample input', status: 'draft' as const, config: { n: 8, seed: 42 } },
    }
    const adhocNode = {
      ...node,
      data: { ...node.data, status: 'draft' as const, config: {
        source: 'adhoc', mode: 'map', code: 'def fn(row): return row',
      } },
    }
    const requestRun = vi.fn().mockResolvedValue(undefined)
    const run = vi.fn().mockResolvedValue(undefined)
    const runEditorPreview = vi.fn().mockResolvedValue(undefined)
    const doc = {
      id: 'canvas', name: 'canvas', version: 1, requirements: [], nodes: [source, upstream, adhocNode],
      edges: [
        { id: 'source-sample', source: 'source', sourceHandle: 'out', target: 'sample', targetHandle: 'in', data: { wire: 'dataset' as const } },
        { id: 'sample-transform', source: 'sample', sourceHandle: 'out', target: 'transform', targetHandle: 'in', data: { wire: 'sample' as const } },
      ],
    }
    useStore.setState({
      doc,
      kernelUp: true,
      fullscreenCode: { nodeId: 'transform', param: 'code', lang: 'python' },
      requestRun, run, runEditorPreview,
      runs: { sample: { phase: 'done', status: { runId: 'baseline-run' } } },
    } as any)

    render(<CodeFullscreen />)

    const runUpstream = await screen.findByRole('button', { name: 'Run upstream' })
    expect(screen.getByRole('button', { name: 'Test code' })).toBeDisabled()
    fireEvent.click(runUpstream)
    fireEvent.click(runUpstream)
    expect(requestRun).toHaveBeenCalledWith('sample')
    expect(requestRun).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('code-editor')).toBeInTheDocument()
    expect(screen.queryByRole('status', { name: 'Upstream run cancelled' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Test code' })).toBeDisabled()

    await act(async () => {
      useStore.setState({ editorPreviews: { transform: {
        canvasId: doc.id,
        nodeId: 'transform',
        planIdentity: previewPlanIdentity(doc, 'transform'),
        parameterBindings: [],
        requestGeneration: 1,
        offset: 0,
        result: {
          columns: [], rows: [], truncated: false,
          editorTestInput: {
            runId: 'baseline-run', nodeId: 'sample', portId: 'out', label: 'Sample input', rows: 8,
          },
        },
      } } } as any)
    })
    expect(screen.getByRole('button', { name: 'Test code' })).toBeDisabled()
    await act(async () => {
      useStore.setState({ editorPreviews: {} } as any)
    })

    await act(async () => {
      useStore.setState({ runs: { sample: { phase: 'confirm', estimate: { rows: 2_001 } } } } as any)
    })
    const confirmation = screen.getByRole('region', { name: 'Confirm upstream run' })
    expect(confirmation).toHaveTextContent('2,001 rows')
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('status', { name: 'Upstream run cancelled' })).toBeInTheDocument()
    expect(screen.getByTestId('code-editor')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Test code' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Run upstream' }))
    expect(requestRun).toHaveBeenCalledTimes(2)
    await act(async () => {
      useStore.setState({ runs: { sample: { phase: 'confirm', estimate: { rows: 2_001 } } } } as any)
    })
    const retriedConfirmation = screen.getByRole('region', { name: 'Confirm upstream run' })
    const confirmRun = within(retriedConfirmation).getByRole('button', { name: 'Run upstream' })
    fireEvent.click(confirmRun)
    fireEvent.click(confirmRun)
    expect(run).toHaveBeenCalledWith('sample', true)
    expect(run).toHaveBeenCalledTimes(1)

    await act(async () => {
      useStore.setState({ runs: { sample: { phase: 'running', status: { runId: 'upstream-run', rowsProcessed: 4, totalRows: 8, progress: 0.5 } } } } as any)
    })
    expect(screen.getByRole('status', { name: 'Upstream run progress' })).toHaveTextContent('4 / 8 rows')

    await act(async () => {
      useStore.setState({ runs: { sample: { phase: 'done', status: { runId: 'upstream-run' } } } } as any)
    })
    expect(screen.getByRole('status', { name: 'Upstream run progress' })).toHaveTextContent('Selecting fresh upstream result')
    expect(screen.getByRole('button', { name: 'Test code' })).toBeDisabled()
    await waitFor(() => expect(runEditorPreview).toHaveBeenCalledWith('transform'))
    await act(async () => {
      useStore.setState({ editorPreviews: { transform: {
        canvasId: doc.id,
        nodeId: 'transform',
        planIdentity: previewPlanIdentity(doc, 'transform'),
        parameterBindings: [],
        requestGeneration: 1,
        offset: 0,
        result: {
          columns: [], rows: [], truncated: false,
          editorTestInput: {
            runId: 'upstream-run', nodeId: 'sample', portId: 'out', label: 'Sample input', rows: 8,
          },
        },
      } } } as any)
    })
    expect(screen.queryByRole('status', { name: 'Upstream result ready' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Test code' })).toBeEnabled()

    // The retained editor input is authoritative even if the run-status channel lags behind the
    // graph result. A real provider run can reach this state until the dialog is reopened.
    await act(async () => {
      useStore.setState({ runs: { sample: {
        phase: 'running',
        status: { runId: 'upstream-run', rowsProcessed: 8, totalRows: 8, progress: 1 },
      } } } as any)
    })
    expect(screen.queryByRole('status', { name: 'Upstream run progress' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Test code' })).toBeEnabled()

    useStore.getState().updateConfig('transform', { code: 'def fn(row): return {**row, "edited": True}' })
    expect(screen.getByRole('button', { name: 'Test code' })).toBeEnabled()

    await act(async () => {
      useStore.setState({ runs: { sample: { phase: 'failed', error: 'upstream fixture failed' } } } as any)
    })
    expect(screen.getByRole('alert', { name: 'Upstream run failed' })).toHaveTextContent('upstream fixture failed')
    expect(screen.getByTestId('code-editor')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Test code' })).toBeDisabled()
  })

  it('does not apply an upstream completion after the fullscreen editor changes nodes', async () => {
    const source = {
      id: 'source', type: 'source', position: { x: 0, y: 0 },
      data: { title: 'Source input', status: 'draft' as const, config: { uri: 'events.parquet' } },
    }
    const upstream = {
      id: 'sample', type: 'sample', position: { x: 0, y: 0 },
      data: { title: 'Sample input', status: 'draft' as const, config: { n: 8, seed: 42 } },
    }
    const transform = (id: string) => ({
      ...node,
      id,
      data: { ...node.data, status: 'draft' as const, config: {
        source: 'adhoc', mode: 'map', code: `def fn(row): return {**row, "${id}": True}`,
      } },
    })
    const first = transform('first-transform')
    const second = transform('second-transform')
    const doc = {
      id: 'canvas', name: 'canvas', version: 1, requirements: [],
      nodes: [source, upstream, first, second],
      edges: [
        { id: 'source-sample', source: 'source', sourceHandle: 'out', target: 'sample', targetHandle: 'in', data: { wire: 'dataset' as const } },
        { id: 'sample-first', source: 'sample', sourceHandle: 'out', target: first.id, targetHandle: 'in', data: { wire: 'sample' as const } },
        { id: 'sample-second', source: 'sample', sourceHandle: 'out', target: second.id, targetHandle: 'in', data: { wire: 'sample' as const } },
      ],
    }
    const requestRun = vi.fn().mockResolvedValue(undefined)
    const runEditorPreview = vi.fn().mockResolvedValue(undefined)
    useStore.setState({
      doc,
      kernelUp: true,
      fullscreenCode: { nodeId: first.id, param: 'code', lang: 'python' },
      requestRun,
      runEditorPreview,
      runs: {},
    } as any)

    render(<CodeFullscreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Run upstream' }))
    await act(async () => {
      useStore.setState({
        fullscreenCode: { nodeId: second.id, param: 'code', lang: 'python' },
        runs: { sample: { phase: 'done', status: { runId: 'late-first-run' } } },
      } as any)
    })

    await waitFor(() => expect(screen.getByTestId('code-editor')).toBeInTheDocument())
    expect(runEditorPreview).not.toHaveBeenCalled()
    expect(screen.queryByRole('status', { name: 'Upstream result ready' })).not.toBeInTheDocument()
  })
})
