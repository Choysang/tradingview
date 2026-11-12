/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';
import { mockApiPlugin } from './mock/plugin.ts';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  const useMock = env.VITE_USE_MOCK === '1';
  const target = env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8200';
  return {
    plugins: [react(), ...(useMock ? [mockApiPlugin()] : [])],
    server: {
      port: 5173,
      // Real mode: the only upstream path is the versioned contract surface.
      proxy: useMock ? undefined : { '/v1': { target, changeOrigin: true }, '/app-config.json': { target, changeOrigin: true } },
    },
    preview: { port: 4173, proxy: useMock ? undefined : { '/v1': { target, changeOrigin: true }, '/app-config.json': { target, changeOrigin: true } } },
    build: { sourcemap: false, chunkSizeWarningLimit: 900 },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./tests/setup.ts'],
      include: ['tests/**/*.test.{ts,tsx}'],
      css: false,
    },
  };
});
