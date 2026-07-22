import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  agentStatus: vi.fn(),
  agentAct: vi.fn(),
  pushAgent: vi.fn(),
  setAgentOpen: vi.fn(),
  applyAgentGraph: vi.fn(),
  requestRun: vi.fn(),
  runPreview: vi.fn(),
  state: {
    agentOpen: true,
    agentLog: [] as { role: 'user' | 'agent'; text: string; plan?: string[] }[],
    canvasRole: 'owner' as 'owner' | 'editor' | 'viewer' | null,
    doc: { id: 'canvas-1', version: 1, nodes: [], edges: [] },
  },
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      agentStatus: mocks.agentStatus,
      agentAct: mocks.agentAct,
    },
  }
})

vi.mock('../store/graph', () => ({
  roleCanEdit: (role: string | null | undefined) => role === 'owner' || role === 'editor',
  useStore: Object.assign(
    (selector: (value: typeof mocks.state & {
      pushAgent: typeof mocks.pushAgent
      setAgentOpen: typeof mocks.setAgentOpen
      applyAgentGraph: typeof mocks.applyAgentGraph
      requestRun: typeof mocks.requestRun
      runPreview: typeof mocks.runPreview
    }) => unknown) => selector({
      ...mocks.state,
      pushAgent: mocks.pushAgent,
      setAgentOpen: mocks.setAgentOpen,
      applyAgentGraph: mocks.applyAgentGraph,
      requestRun: mocks.requestRun,
      runPreview: mocks.runPreview,
    }),
    {
      getState: () => ({
        ...mocks.state,
        pushAgent: mocks.pushAgent,
        setAgentOpen: mocks.setAgentOpen,
        applyAgentGraph: mocks.applyAgentGraph,
        requestRun: mocks.requestRun,
        runPreview: mocks.runPreview,
      }),
    },
  ),
}))

import { AgentDock } from './AgentDock'

describe('AgentDock — AgentDataPolicy preflight disclosure', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.agentOpen = true
    mocks.state.agentLog = []
    mocks.state.canvasRole = 'owner'
    mocks.state.doc = { id: 'canvas-1', version: 1, nodes: [], edges: [] }
    mocks.agentStatus.mockResolvedValue({
      available: true,
      reason: '',
      model: 'anthropic/claude-opus-4-8',
      provider: 'anthropic',
      disclosure: {
        provider: 'anthropic',
        model: 'anthropic/claude-opus-4-8',
        level: 'metadata-only',
        endpointIsLocal: false,
        hosted: true,
        rowValuesMayLeave: false,
      },
    })
  })

  it('renders provider/model and whether row values may leave before the first message', async () => {
    render(<AgentDock />)

    expect(await screen.findByTestId('agent-egress-disclosure')).toBeInTheDocument()
    expect(screen.getByTestId('agent-disclosure-provider')).toHaveTextContent('anthropic')
    expect(screen.getByTestId('agent-disclosure-model')).toHaveTextContent('anthropic/claude-opus-4-8')
    expect(screen.getByTestId('agent-disclosure-values')).toHaveTextContent(
      /Sample row values will not leave this deployment/,
    )
    expect(screen.getByTestId('agent-disclosure-values')).not.toHaveTextContent(
      /Sample row values may leave/,
    )
  })

  it('states when sample values may leave under an opted-in policy', async () => {
    mocks.agentStatus.mockResolvedValue({
      available: true,
      reason: '',
      model: 'openai/gpt-5',
      provider: 'openai',
      disclosure: {
        provider: 'openai',
        model: 'openai/gpt-5',
        level: 'sample-values',
        endpointIsLocal: false,
        hosted: true,
        rowValuesMayLeave: true,
      },
    })
    render(<AgentDock />)
    expect(await screen.findByTestId('agent-disclosure-values')).toHaveTextContent(
      /Sample row values may leave this deployment/,
    )
  })

  it('keeps the standalone-request disclosure visible after a completed request', async () => {
    mocks.state.agentLog = [{ role: 'user', text: 'build a filter' }]
    render(<AgentDock />)
    expect(await screen.findByTestId('agent-egress-disclosure')).toBeInTheDocument()
    expect(screen.getByText(/Earlier requests and results shown here are display-only/)).toBeInTheDocument()
    expect(screen.getByTestId('agent-completed-request')).toHaveTextContent('build a filter')
  })

  it('submits only the current prompt and graph, then clears the next request prompt', async () => {
    mocks.state.doc = {
      id: 'canvas-1', version: 1, nodes: [
        { id: 'source', type: 'source', position: { x: 0, y: 0 }, data: { config: {} } },
        { id: 'note', type: 'note', position: { x: 1, y: 1 }, data: { config: {} } },
      ],
      edges: [{ id: 'edge', source: 'source', target: 'note' }],
    } as any
    mocks.agentAct.mockResolvedValue({ available: true, graph: { nodes: [], edges: [] }, transcript: [], summary: 'Done.' })
    render(<AgentDock />)

    expect(await screen.findByTestId('agent-request-context')).toHaveTextContent('1 dataflow node and 0 connections')
    const input = screen.getByPlaceholderText('Describe this request…')
    fireEvent.change(input, { target: { value: 'build a filter' } })
    fireEvent.click(screen.getByTestId('agent-submit'))

    await waitFor(() => expect(mocks.agentAct).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'canvas-1' }),
      'build a filter',
    ))
    expect(mocks.agentAct).toHaveBeenCalledTimes(1)
    expect(input).toHaveValue('')
  })

  it('ignores a late Agent response after switching canvases', async () => {
    let finishRequest!: (result: {
      available: boolean
      graph: { nodes: never[]; edges: never[] }
      transcript: { tool: string; input: { kind: string }; result: Record<string, never> }[]
      summary: string
    }) => void
    mocks.agentAct.mockImplementationOnce(() => new Promise((resolve) => { finishRequest = resolve }))
    render(<AgentDock />)

    const input = await screen.findByPlaceholderText('Describe this request…')
    fireEvent.change(input, { target: { value: 'build on canvas one' } })
    fireEvent.click(screen.getByTestId('agent-submit'))
    await waitFor(() => expect(mocks.agentAct).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'canvas-1' }),
      'build on canvas one',
    ))

    mocks.state.doc = { id: 'canvas-2', version: 1, nodes: [], edges: [] }
    await act(async () => {
      finishRequest({
        available: true,
        graph: { nodes: [], edges: [] },
        transcript: [{ tool: 'add_node', input: { kind: 'source' }, result: {} }],
        summary: 'Updated the canvas.',
      })
    })

    expect(mocks.applyAgentGraph).not.toHaveBeenCalled()
    expect(mocks.pushAgent).toHaveBeenCalledTimes(1)
    expect(mocks.pushAgent).toHaveBeenCalledWith({ role: 'user', text: 'build on canvas one' })
  })

  it('still shows the configure affordance when the agent is unavailable', async () => {
    mocks.agentStatus.mockResolvedValue({
      available: false,
      errorCode: 'agent_credential_unavailable',
      reason: 'The configured Agent credential is unavailable. Update it or clear the selection in Settings.',
      model: 'anthropic/claude-opus-4-8',
    })
    render(<AgentDock />)
    expect(await screen.findByTestId('agent-configure')).toBeInTheDocument()
    expect(screen.getByText(/configured Agent credential is unavailable/)).toBeInTheDocument()
    expect(screen.queryByTestId('agent-egress-disclosure')).toBeNull()
    fireEvent.click(screen.getByTestId('agent-configure'))
  })
})
