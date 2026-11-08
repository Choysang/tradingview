import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// PULSE_API lets a second dev server talk to a different backend (for example a
// LIVE instance on :8010) without editing this file.
const target = process.env.PULSE_API ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target, changeOrigin: true },
      '/mcp': { target, changeOrigin: true },
      '/llms.txt': { target, changeOrigin: true },
    },
  },
  build: {
    target: 'es2022',
    cssMinify: true,
  },
})
