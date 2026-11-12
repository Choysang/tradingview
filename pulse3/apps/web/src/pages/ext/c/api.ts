/**
 * HTTP helper for the extension operations (/v1/x/<kebab-op>) and the support routes (/v1/x/c/*).
 * The generated openapi-fetch client only knows the 31 core operations, so this mimics its error handling:
 * every non-2xx becomes an ApiError (status, code, field_errors, meta), network failures become status 0.
 * Auth is the same-origin cookie session, exactly like client.ts (fetch default credentials, stated explicitly).
 * Conventions decided by the backend: GET -> each request field is a query param (objects/arrays as JSON text,
 * null/undefined omitted); POST -> JSON body plus the Idempotency-Key header.
 */
import { ApiError, newIdempotencyKey } from '../../../api/client';
import type { Envelope, ErrorBody, ResponseMeta } from '../../../api/types';
import { API_ORIGIN } from '../../../config';
import type { ArtifactData, ArtifactRef, JobRun } from './types';

/** All-zero UUID = 「latest snapshot」 for snapshot_id inputs. */
export const ZERO_UUID = '00000000-0000-0000-0000-000000000000';

export type Params = Record<string, unknown>;

/** Encode one query value: strings/numbers/booleans as plain text, objects/arrays as JSON text. */
export function encodeQueryValue(v: unknown): string {
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

export function encodeQuery(params: Params): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    sp.append(k, encodeQueryValue(v));
  }
  return sp.toString();
}

type ErrBody = { error?: ErrorBody; meta?: ResponseMeta } | null;
const asErrBody = (b: unknown): ErrBody => (typeof b === 'object' && b !== null ? (b as ErrBody) : null);

interface RequestInit2 { query?: Params; body?: unknown; idempotencyKey?: string; signal?: AbortSignal }

async function request<T>(method: 'GET' | 'POST', path: string, init: RequestInit2, prefix = '/v1/x/'): Promise<Envelope<T>> {
  const qs = init.query ? encodeQuery(init.query) : '';
  const url = `${API_ORIGIN}${prefix}${path}${qs ? `?${qs}` : ''}`;
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (method === 'POST') {
    headers['Content-Type'] = 'application/json';
    headers['Idempotency-Key'] = init.idempotencyKey ?? newIdempotencyKey();
  }
  let res: Response;
  try {
    res = await fetch(url, { method, headers, credentials: 'same-origin', body: method === 'POST' ? JSON.stringify(init.body ?? {}) : undefined, signal: init.signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, null, '网络不可用或服务无响应');
  }
  let body: unknown = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) throw new ApiError(res.status, asErrBody(body));
  if (typeof body !== 'object' || body === null || !('data' in body)) throw new ApiError(res.status, null, '服务返回了无法识别的内容');
  return body as Envelope<T>;
}

export const extGet = <T>(path: string, query?: Params, signal?: AbortSignal): Promise<Envelope<T>> => request<T>('GET', path, { query, signal });
export const extPost = <T>(path: string, body: unknown, idempotencyKey?: string): Promise<Envelope<T>> => request<T>('POST', path, { body, idempotencyKey });

/** Core-API job run (getJobRun, GET /v1/jobs/{id}); the receipt's poll_operation_id points here. */
export const getJobRun = (id: string, signal?: AbortSignal): Promise<Envelope<JobRun>> => request<JobRun>('GET', encodeURIComponent(id), { signal }, '/v1/jobs/');

export async function fetchArtifact(ref: ArtifactRef | string, signal?: AbortSignal): Promise<Envelope<ArtifactData>> {
  return extGet<ArtifactData>('c/artifact', { ref: typeof ref === 'string' ? ref : ref.ref }, signal);
}

/** Normalised error code: bare HTTP statuses without a typed body are mapped to the typed codes. */
export function errorCode(e: unknown): string {
  if (!(e instanceof ApiError)) return 'UNKNOWN';
  if (e.code === 'HTTP_401') return 'UNAUTHORIZED';
  if (e.code === 'HTTP_403') return 'FORBIDDEN';
  if (e.code === 'HTTP_404') return 'NOT_FOUND';
  return e.code;
}

export const isNotReady = (e: unknown): boolean => errorCode(e) === 'RESULT_NOT_READY';

/** Chinese message for an error; field errors are appended so the user sees which input is wrong. */
export function explainError(e: unknown): string {
  if (!(e instanceof ApiError)) return e instanceof Error ? e.message : '未知错误';
  const code = errorCode(e);
  const fields = e.fieldErrors.length > 0 ? `：${e.fieldErrors.map((f) => `${f.path} ${f.message}`).join('；')}` : '';
  switch (code) {
    case 'UNAUTHORIZED': case 'AUTH_REQUIRED': return '需要先登录，请登录后重试';
    case 'FORBIDDEN': return '没有权限执行此操作';
    case 'VERSION_CONFLICT': return '内容已被更新（版本冲突），请刷新后再试';
    case 'IDEMPOTENCY_CONFLICT': return '同一个请求编号对应了不同的内容，请重新提交';
    case 'RESULT_NOT_READY': return '结果尚未生成';
    case 'NOT_FOUND': return '没有找到对应内容（或不属于当前账号）';
    case 'CONTRACT_VIOLATION': case 'BAD_REQUEST': return `请求参数不符合要求${fields}`;
    default: return `${e.message}${fields}`;
  }
}

/* ---------- canonical JSON + inline parameter artifact (runRegisteredJob) ---------- */

function cmpCodePoint(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i += 1) {
    const d = (x[i] as string).codePointAt(0)! - (y[i] as string).codePointAt(0)!;
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

/** JSON with sorted keys, no whitespace, non-ASCII kept as-is (UTF-8). Throws on non-finite numbers / unsupported values. */
export function canonicalJson(v: unknown): string {
  if (v === null) return 'null';
  switch (typeof v) {
    case 'string': return JSON.stringify(v);
    case 'boolean': return v ? 'true' : 'false';
    case 'number':
      if (!Number.isFinite(v)) throw new Error('JSON 不支持 NaN / Infinity');
      return JSON.stringify(v);
    case 'object': {
      if (Array.isArray(v)) return `[${v.map(canonicalJson).join(',')}]`;
      const o = v as Record<string, unknown>;
      const keys = Object.keys(o).filter((k) => o[k] !== undefined).sort(cmpCodePoint);
      return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(o[k])}`).join(',')}}`;
    }
    default: throw new Error('JSON 不支持该类型的值');
  }
}

export function base64Url(bytes: Uint8Array): string {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes as BufferSource);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

export const JOB_PARAMETER_CONTRACT_ID = 'pulse.job_parameters';

/** ArtifactRef for an inline-json job parameter object (contract id/version come from the job definition when given); content_hash = sha256 of the canonical UTF-8 bytes. */
export async function buildParameterArtifact(parameters: unknown, contractId: string | null = null, schemaVersion: string | null = null): Promise<ArtifactRef> {
  const bytes = new TextEncoder().encode(canonicalJson(parameters));
  return { ref: `inline-json:${base64Url(bytes)}`, content_hash: await sha256Hex(bytes), contract_id: contractId ?? JOB_PARAMETER_CONTRACT_ID, schema_version: schemaVersion ?? '1' };
}
