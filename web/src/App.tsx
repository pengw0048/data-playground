import { useEffect } from 'react'
import { ReactFlowProvider } from '@xyflow/react'
import { Canvas } from './canvas/Canvas'
import { TopBar } from './canvas/TopBar'
import { Toolbar } from './canvas/Toolbar'
import { AgentDock } from './panels/AgentDock'
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
        <div style={{ position: 'absolute', inset: 0, overflow: 'hidden' }}>
          <Canvas />
          <TopBar />
          <Toolbar />
          <AgentDock />
        </div>
      </ErrorBoundary>
    </ReactFlowProvider>
  )
}
