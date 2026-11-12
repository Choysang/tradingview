/**
 * Vite plugin: serves the synthetic mock for /v1/* and /__mock/* (dev + preview).
 * Enabled only when VITE_USE_MOCK=1; real mode proxies /v1 instead (vite.config.ts).
 */
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Plugin } from 'vite';
import { createMockState, handle } from './handler.ts';

function readBody(req: IncomingMessage): Promise<unknown> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    req.on('data', (c: Buffer) => chunks.push(c));
    req.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf-8');
      if (!text) return resolve(undefined);
      try { resolve(JSON.parse(text)); } catch { resolve(undefined); }
    });
  });
}

export function mockApiPlugin(): Plugin {
  const state = createMockState();
  const middleware = async (req: IncomingMessage, res: ServerResponse, next: () => void): Promise<void> => {
    const url = new URL(req.url ?? '/', 'http://localhost');
    if (!url.pathname.startsWith('/v1/') && !url.pathname.startsWith('/__mock/')) return next();
    const body = req.method === 'GET' || req.method === 'HEAD' ? undefined : await readBody(req);
    const headers: Record<string, string | undefined> = {};
    for (const [k, v] of Object.entries(req.headers)) headers[k.toLowerCase()] = Array.isArray(v) ? v[0] : v;
    const out = handle(state, { method: req.method ?? 'GET', path: url.pathname, query: url.searchParams, headers, body });
    res.statusCode = out.status;
    res.setHeader('content-type', 'application/json; charset=utf-8');
    res.setHeader('cache-control', 'no-store');
    res.end(JSON.stringify(out.body));
  };
  return {
    name: 'pulse3-mock-api',
    configureServer(server) { server.middlewares.use(middleware); },
    configurePreviewServer(server) { server.middlewares.use(middleware); },
  };
}
