import { StrictMode } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  bootstrap: vi.fn(),
  initRouter: vi.fn(),
  settleBootstrap: vi.fn(),
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
import { LOCAL_MODE_CACHE_KEY } from './localIdentity'
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
    mocks.initRouter.mockReset().mockReturnValue({ settleBootstrap: mocks.settleBootstrap })
    mocks.settleBootstrap.mockReset()
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

  it('boots normally for a confirmed authenticated session', async () => {
    vi.spyOn(api, 'authStatus').mockResolvedValue({ authEnabled: true, userId: 'alice' })

    render(<App />)

    await waitFor(() => expect(mocks.bootstrap).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('canvas')).toBeVisible()
    expect(screen.queryByTestId('login')).not.toBeInTheDocument()
    expect(useStore.getState().authEnabled).toBe(true)
  })

  it('installs one router and bootstrap owner under StrictMode effect replay', async () => {
    vi.spyOn(api, 'authStatus').mockResolvedValue({ authEnabled: false, userId: null })
    mocks.bootstrap.mockImplementation(async ({ navigationToken }) => navigationToken)

    render(<StrictMode><App /></StrictMode>)

    await waitFor(() => expect(mocks.bootstrap).toHaveBeenCalledTimes(1))
    expect(mocks.initRouter).toHaveBeenCalledTimes(1)
    const navigationToken = mocks.bootstrap.mock.calls[0][0].navigationToken
    expect(mocks.initRouter).toHaveBeenCalledWith(useStore, navigationToken)
    await waitFor(() => expect(mocks.settleBootstrap).toHaveBeenCalledWith(navigationToken))
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

  it('recovers confirmed local mode drafts while the hub is unavailable on reload', async () => {
    localStorage.setItem(LOCAL_MODE_CACHE_KEY, '1')
    const status = vi.spyOn(api, 'authStatus').mockRejectedValue(new TypeError('hub unavailable'))

    render(<App />)

    await waitFor(() => expect(mocks.bootstrap).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('canvas')).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(status).toHaveBeenCalledTimes(3)
  })
})
