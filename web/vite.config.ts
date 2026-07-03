import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Frontend is a static SPA (NFR-7). In dev it proxies /api and /ws to the kernel.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8471', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8471', ws: true },
    },
  },
})
