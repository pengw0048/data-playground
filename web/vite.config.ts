/// <reference types="vitest/config" />
import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Frontend is a static SPA (NFR-7). In dev it proxies /api and /ws to the kernel.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },  // shadcn '@/…' imports
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8471', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8471', ws: true },
    },
  },
  // Unit/component tests (vitest, jsdom). The real-app end-to-end specs live under e2e/ (Playwright)
  // and are excluded here — `npm test` is fast + serverless, `npm run e2e` drives the built app.
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    exclude: ['e2e/**', 'node_modules/**'],
  },
})
