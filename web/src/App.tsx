import { useEffect } from 'react'
import { ReactFlowProvider } from '@xyflow/react'
import { Canvas } from './canvas/Canvas'
import { TopBar } from './canvas/TopBar'
import { Toolbar } from './canvas/Toolbar'
import { AgentDock } from './panels/AgentDock'
import { Inspector } from './panels/Inspector'
import { useStore } from './store/graph'
import { ErrorBoundary } from './ui/ErrorBoundary'

export default function App() {
  const bootstrap = useStore((s) => s.bootstrap)

  useEffect(() => {
    bootstrap()
  }, [bootstrap])

  return (
    <ReactFlowProvider>
      <ErrorBoundary>
        <div style={{ position: 'absolute', inset: 0, overflow: 'hidden', display: 'flex' }}>
          {/* canvas region (left, flexible) — Canvas fills it; TopBar/Toolbar/AgentDock overlay it */}
          <div style={{ position: 'relative', flex: 1, minWidth: 0 }}>
            <Canvas />
            <TopBar />
            <Toolbar />
            <AgentDock />
          </div>
          {/* persistent right property panel (Figma-style) */}
          <Inspector />
        </div>
      </ErrorBoundary>
    </ReactFlowProvider>
  )
}
