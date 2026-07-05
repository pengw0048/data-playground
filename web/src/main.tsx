import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { initTheme } from './theme/mode'

initTheme()  // apply the saved/OS theme before first paint (no flash), then follow the OS in 'system' mode

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
