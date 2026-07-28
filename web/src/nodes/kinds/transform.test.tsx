import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ReactFlowProvider } from '@xyflow/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'

vi.mock('../../api/client', () => ({
  api: new Proxy({}, { get: () => async () => ({}) }),
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
  DataPanel: ({ editorPreview }: { editorPreview?: { onRunUpstream?: () => void } }) => (
    editorPreview?.onRunUpstream
      ? <button type="button" onClick={editorPreview.onRunUpstream}>Run upstream</button>
      : null
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
    useStore.setState({
      canvasRole: 'owner', fullscreenCode: null, previews: {},
      doc: { id: 'canvas', name: 'canvas', version: 1, requirements: [], nodes: [node], edges: [] },
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

  it('shows an unlisted shared exact ref in the fullscreen read-only label', async () => {
    useStore.setState({
      canvasRole: 'viewer', processors: [],
      fullscreenCode: { nodeId: node.id, param: 'code', lang: 'python' },
    } as any)

    render(<CodeFullscreen />)

    expect(await screen.findByText(new RegExp(`${PROCESSOR_ID}@v1`))).toBeInTheDocument()
    expect(screen.queryByText(/Latest version/)).not.toBeInTheDocument()
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
      requestRun, run, runEditorPreview, runs: {},
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
