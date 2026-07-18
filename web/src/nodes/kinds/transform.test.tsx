import { render, screen } from '@testing-library/react'
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

  it('shows an unlisted shared exact ref in the fullscreen read-only label', async () => {
    useStore.setState({
      canvasRole: 'viewer', processors: [],
      fullscreenCode: { nodeId: node.id, param: 'code', lang: 'python' },
    } as any)

    render(<CodeFullscreen />)

    expect(await screen.findByText(new RegExp(`${PROCESSOR_ID}@v1`))).toBeInTheDocument()
    expect(screen.queryByText(/Latest version/)).not.toBeInTheDocument()
  })
})
