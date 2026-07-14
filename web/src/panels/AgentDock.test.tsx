import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

  it('hides the disclosure once the conversation has started', async () => {
    mocks.state.agentLog = [{ role: 'user', text: 'build a filter' }]
    render(<AgentDock />)
    await waitFor(() => expect(mocks.agentStatus).toHaveBeenCalled())
    expect(screen.queryByTestId('agent-egress-disclosure')).toBeNull()
  })

  it('still shows the configure affordance when the agent is unavailable', async () => {
    mocks.agentStatus.mockResolvedValue({
      available: false,
      reason: 'set ANTHROPIC_API_KEY',
      model: 'anthropic/claude-opus-4-8',
    })
    render(<AgentDock />)
    expect(await screen.findByTestId('agent-configure')).toBeInTheDocument()
    expect(screen.queryByTestId('agent-egress-disclosure')).toBeNull()
    fireEvent.click(screen.getByTestId('agent-configure'))
  })
})
