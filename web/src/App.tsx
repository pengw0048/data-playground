import { useEffect, useState } from 'react'
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
import { TooltipProvider } from '@/components/ui/tooltip'
import { api } from './api/client'
import { useStore } from './store/graph'
import { initRouter } from './router'
import { syncPluginCapabilities } from './nodes/capabilities'
import { ErrorBoundary } from './ui/ErrorBoundary'
import { useCollapsibleRegion } from './layoutPreferences'

export default function App() {
  const bootstrap = useStore((s) => s.bootstrap)
  const view = useStore((s) => s.view)
  // gate on auth: null = checking; then either the login screen (auth on, no session) or the app.
  const [auth, setAuth] = useState<{ authEnabled: boolean; userId: string | null } | null>(null)
  const [booted, setBooted] = useState(false)
  const [inspectorCollapsed, setInspectorCollapsed] = useCollapsibleRegion('inspector')

  useEffect(() => {
    api.authStatus()
      .then((a) => { setAuth(a); useStore.getState().setAuthEnabled(a.authEnabled) })
      .catch(() => setAuth({ authEnabled: false, userId: 'local' }))
  }, [])
  useEffect(() => {
    if (auth && (!auth.authEnabled || auth.userId) && !booted) {
      setBooted(true)
      bootstrap().then(() => {
        initRouter(useStore)  // wire URL ↔ state once the initial canvas is settled
        // register generic viewer tabs for plugin capabilities that declare one (§5.6, no per-plugin FE code)
        syncPluginCapabilities(useStore.getState().kernelInfo?.capabilityViews ?? [])
      })
    }
  }, [auth, booted, bootstrap])

  if (!auth) return <div style={{ position: 'absolute', inset: 0 }} />  // brief splash while checking auth
  if (auth.authEnabled && !auth.userId) return <Login onLoggedIn={(uid) => setAuth({ authEnabled: true, userId: uid })} />

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
