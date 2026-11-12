/**
 * Typed HTTP client generated from contracts/openapi.json (openapi-typescript +
 * openapi-fetch). Only the 31 contract operations under /v1 are reachable.
 * Why: spec 11 - structured errors drive UI state; the page never parses prose.
 * Invariants: every non-2xx becomes an ApiError carrying the typed Error body and
 * response meta when present. Not covered: caching (TanStack Query, display only).
 */
import createClient from 'openapi-fetch';
import type { paths } from './schema';
import type { ErrorBody, ResponseMeta } from './types';
import { API_ORIGIN } from '../config';

export const http = createClient<paths>({ baseUrl: API_ORIGIN });

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly fieldErrors: ErrorBody['field_errors'];
  readonly meta: ResponseMeta | null;

  constructor(status: number, body: { error?: ErrorBody; meta?: ResponseMeta } | null, fallback?: string) {
    super(body?.error?.message ?? fallback ?? `请求失败（HTTP ${status}）`);
    this.name = 'ApiError';
    this.status = status;
    this.code = body?.error?.code ?? (status === 0 ? 'NETWORK' : `HTTP_${status}`);
    this.retryable = body?.error?.retryable ?? (status === 0 || status >= 500);
    this.fieldErrors = body?.error?.field_errors ?? [];
    this.meta = body?.meta ?? null;
  }
}

interface FetchResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Unwrap an openapi-fetch result: return data or throw ApiError. */
export function unwrap<T>(res: FetchResult<T>): T {
  if (res.data !== undefined && res.response.ok) return res.data;
  const body = (typeof res.error === 'object' && res.error !== null ? res.error : null) as { error?: ErrorBody; meta?: ResponseMeta } | null;
  throw new ApiError(res.response.status, body);
}

/** Wrap network-level failures (fetch rejected) as ApiError(status 0). */
export async function guard<T>(run: () => Promise<FetchResult<T>>): Promise<T> {
  let res: FetchResult<T>;
  try {
    res = await run();
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, null, '网络不可用或服务无响应');
  }
  return unwrap(res);
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
