import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  bootstrap: vi.fn(),
  initRouter: vi.fn(),
  syncPluginCapabilities: vi.fn(),
}))

const storage = new Map<string, string>()

vi.mock('@xyflow/react', () => ({ ReactFlowProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> }))
vi.mock('./canvas/Canvas', () => ({ Canvas: () => <div data-testid="canvas">Canvas</div> }))
vi.mock('./canvas/TopBar', () => ({ TopBar: () => null }))
vi.mock('./canvas/Toolbar', () => ({ Toolbar: () => null }))
vi.mock('./panels/AgentDock', () => ({ AgentDock: () => null }))
vi.mock('./panels/Inspector', () => ({ Inspector: () => null }))
vi.mock('./panels/CodeFullscreen', () => ({ CodeFullscreen: () => null }))
vi.mock('./views/Shell', () => ({ Shell: () => <div>Shell</div> }))
vi.mock('./views/Login', () => ({ Login: () => <div data-testid="login">Login</div> }))
vi.mock('./ui/Toaster', () => ({ Toaster: () => null }))
vi.mock('./router', () => ({ initRouter: mocks.initRouter }))
vi.mock('./nodes/capabilities', () => ({ syncPluginCapabilities: mocks.syncPluginCapabilities }))

import App from './App'
import { api, KernelError } from './api/client'
import { useStore } from './store/graph'

describe('App auth bootstrap', () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: {
        clear: () => storage.clear(),
        getItem: (key: string) => storage.get(key) ?? null,
        removeItem: (key: string) => { storage.delete(key) },
        setItem: (key: string, value: string) => { storage.set(key, String(value)) },
      } satisfies Storage,
    })
    vi.restoreAllMocks()
    mocks.bootstrap.mockReset().mockResolvedValue(undefined)
    useStore.setState({ bootstrap: mocks.bootstrap, view: 'canvas', authEnabled: false } as never)
    localStorage.clear()
  })

  afterEach(() => { vi.unstubAllGlobals() })

  it.each([
    ['a network failure', new TypeError('network unavailable')],
    ['a 5xx server failure', new KernelError(503, 'Service Unavailable')],
    ['an incompatible response', { authEnabled: 'false', userId: 'local' }],
  ])('does not infer local mode from %s', async (_case, outcome) => {
    localStorage.setItem('dp-canvas', JSON.stringify({ id: 'stale', nodes: [{}] }))
    const status = vi.spyOn(api, 'authStatus')
    if (outcome instanceof Error) status.mockRejectedValue(outcome)
    else status.mockResolvedValue(outcome as never)

    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent('could not confirm whether this server uses local or signed-in access')
    expect(screen.getByText(/Local Canvas drafts remain in this browser/i)).toBeVisible()
    expect(screen.queryByTestId('canvas')).not.toBeInTheDocument()
    expect(mocks.bootstrap).not.toHaveBeenCalled()
    expect(status).toHaveBeenCalledTimes(3)
  })

  it('enters local mode only after a confirmed local response', async () => {
    vi.spyOn(api, 'authStatus').mockResolvedValue({ authEnabled: false, userId: null })

    render(<App />)

    await waitFor(() => expect(mocks.bootstrap).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('canvas')).toBeVisible()
    expect(useStore.getState().authEnabled).toBe(false)
  })

  it('shows login for an authenticated deployment without a session', async () => {
    vi.spyOn(api, 'authStatus').mockResolvedValue({ authEnabled: true, userId: null })

    render(<App />)

    expect(await screen.findByTestId('login')).toBeVisible()
    expect(mocks.bootstrap).not.toHaveBeenCalled()
    expect(useStore.getState().authEnabled).toBe(true)
  })

  it('recovers from an unavailable bootstrap when retry confirms local mode', async () => {
    const status = vi.spyOn(api, 'authStatus')
      .mockRejectedValueOnce(new TypeError('network unavailable'))
      .mockRejectedValueOnce(new TypeError('network unavailable'))
      .mockRejectedValueOnce(new TypeError('network unavailable'))
      .mockResolvedValue({ authEnabled: false, userId: null })

    render(<App />)

    expect(await screen.findByRole('alert')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Retry connection' }))

    await waitFor(() => expect(mocks.bootstrap).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('canvas')).toBeVisible()
    expect(status).toHaveBeenCalledTimes(4)
  })
})
