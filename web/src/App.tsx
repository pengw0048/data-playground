import { useCallback, useEffect, useRef, useState } from 'react'
import { ReactFlowProvider } from '@xyflow/react'
import { Canvas } from './canvas/Canvas'
import { TopBar } from './canvas/TopBar'
import { Toolbar } from './canvas/Toolbar'
import { AgentDock } from './panels/AgentDock'
import { Inspector } from './panels/Inspector'
import { CodeFullscreen } from './panels/CodeFullscreen'
import { Shell } from './views/Shell'
import { Login } from './views/Login'
import { Toaster } from './ui/Toaster'
import { Button } from '@/components/ui/button'
import { TooltipProvider } from '@/components/ui/tooltip'
import { api } from './api/client'
import { useStore } from './store/graph'
import { initRouter } from './router'
import { syncPluginCapabilities } from './nodes/capabilities'
import { ErrorBoundary } from './ui/ErrorBoundary'
import { useCollapsibleRegion } from './layoutPreferences'
import { confirmedLocalMode, rememberAuthMode } from './localIdentity'

type AuthState =
  | { kind: 'checking' }
  | { kind: 'local' }
  | { kind: 'authenticated'; userId: string }
  | { kind: 'login' }
  | { kind: 'unavailable'; attempts: number; diagnostic: string }

const AUTH_BOOTSTRAP_ATTEMPTS = 3
const AUTH_RETRY_DELAY_MS = 250

function isAuthStatus(value: unknown): value is { authEnabled: boolean; userId: string | null } {
  if (!value || typeof value !== 'object') return false
  const status = value as Record<string, unknown>
  return typeof status.authEnabled === 'boolean' && (typeof status.userId === 'string' || status.userId === null)
}

function authDiagnostic(error: unknown) {
  if (error instanceof Error && error.message) return error.message
  return 'The auth status response is incompatible with this app version.'
}

function AuthBootstrapUnavailable({ state, onRetry }: {
  state: Extract<AuthState, { kind: 'unavailable' }>
  onRetry: () => void
}) {
  return (
    <main className="absolute inset-0 grid place-items-center bg-background p-6">
      <section className="w-full max-w-md rounded-lg border border-border bg-card p-6 shadow-lg" aria-labelledby="auth-bootstrap-title">
        <h1 id="auth-bootstrap-title" className="text-base font-semibold text-foreground">Connection unavailable</h1>
        <p role="alert" className="mt-2 text-sm leading-6 text-muted-foreground">
          Data Playground could not confirm whether this server uses local or signed-in access. Local Canvas drafts remain in this browser, but server identity, permissions, and Canvas state are unknown.
        </p>
        <p className="mt-3 text-xs text-muted-foreground">Checked {state.attempts} times. Last attempt: {state.diagnostic}</p>
        <Button className="mt-5" onClick={onRetry}>Retry connection</Button>
      </section>
    </main>
  )
}

export default function App() {
  const bootstrap = useStore((s) => s.bootstrap)
  const view = useStore((s) => s.view)
  // Gate on an explicit auth outcome. An unavailable request is not evidence that this is local mode.
  const [auth, setAuth] = useState<AuthState>({ kind: 'checking' })
  const [booted, setBooted] = useState(false)
  const [inspectorCollapsed, setInspectorCollapsed] = useCollapsibleRegion('inspector')
  const requestGeneration = useRef(0)

  const checkAuth = useCallback(async () => {
    const generation = ++requestGeneration.current
    setAuth({ kind: 'checking' })
    let diagnostic = 'Auth status is unavailable.'
    for (let attempt = 1; attempt <= AUTH_BOOTSTRAP_ATTEMPTS; attempt += 1) {
      try {
        const status: unknown = await api.authStatus()
        if (!isAuthStatus(status)) throw new Error('The auth status response is incompatible with this app version.')
        if (generation !== requestGeneration.current) return
        useStore.getState().setAuthEnabled(status.authEnabled)
        rememberAuthMode(status.authEnabled)
        setAuth(!status.authEnabled ? { kind: 'local' }
          : status.userId ? { kind: 'authenticated', userId: status.userId } : { kind: 'login' })
        return
      } catch (error) {
        diagnostic = authDiagnostic(error)
        if (attempt < AUTH_BOOTSTRAP_ATTEMPTS) {
          await new Promise((resolve) => window.setTimeout(resolve, AUTH_RETRY_DELAY_MS))
          if (generation !== requestGeneration.current) return
        }
      }
    }
    if (generation === requestGeneration.current) {
      // A previously server-confirmed no-auth local deployment has one stable workstation principal.
      // Re-enter only that mode while the hub is down so its Canvas drafts can survive a full reload.
      // Signed-in deployments never use this fallback: identity/logout must be re-confirmed online.
      if (confirmedLocalMode()) setAuth({ kind: 'local' })
      else setAuth({ kind: 'unavailable', attempts: AUTH_BOOTSTRAP_ATTEMPTS, diagnostic })
    }
  }, [])

  useEffect(() => {
    void checkAuth()
    return () => { requestGeneration.current += 1 }
  }, [checkAuth])
  useEffect(() => {
    if ((auth.kind === 'local' || auth.kind === 'authenticated') && !booted) {
      setBooted(true)
      bootstrap().then(() => {
        initRouter(useStore)  // wire URL ↔ state once the initial canvas is settled
        // register generic viewer tabs for plugin capabilities that declare one (§5.6, no per-plugin FE code)
        syncPluginCapabilities(useStore.getState().kernelInfo?.capabilityViews ?? [])
      })
    }
  }, [auth, booted, bootstrap])

  if (auth.kind === 'checking') return <div style={{ position: 'absolute', inset: 0 }} />  // brief splash while checking auth
  if (auth.kind === 'unavailable') return <AuthBootstrapUnavailable state={auth} onRetry={() => void checkAuth()} />
  if (auth.kind === 'login') return <Login onLoggedIn={(userId) => setAuth({ kind: 'authenticated', userId })} />

  return (
    <TooltipProvider delayDuration={300}>
    <ReactFlowProvider>
      <ErrorBoundary>
        {view === 'canvas' ? (
          <div style={{ position: 'absolute', inset: 0, overflow: 'hidden', display: 'flex' }}>
            {/* canvas region (left, flexible) — Canvas fills it; TopBar/Toolbar/AgentDock overlay it */}
            <div style={{ position: 'relative', flex: 1, minWidth: 0 }}>
              <Canvas />
              <TopBar />
              <Toolbar />
              <AgentDock />
            </div>
            {/* collapsible right property panel (Figma-style) */}
            <Inspector collapsed={inspectorCollapsed} onToggle={() => setInspectorCollapsed((value) => !value)} />
          </div>
        ) : (
          <Shell />
        )}
        <CodeFullscreen />
        <Toaster />
      </ErrorBoundary>
    </ReactFlowProvider>
    </TooltipProvider>
  )
}
