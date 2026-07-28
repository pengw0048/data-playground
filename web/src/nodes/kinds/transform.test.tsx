import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
vi.mock('../../panels/DataPanel', () => ({ DataPanel: () => null }))

import './transform'
import { getComponent } from '../registry'
import { useStore } from '../../store/graph'
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
})
