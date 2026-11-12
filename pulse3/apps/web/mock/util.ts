/**
 * Mock layer helpers. Everything served by /mock is SYNTHETIC: contract-valid shapes
 * with hypothetical values, always carrying the SYNTHETIC_FIXTURE warning
 * (spec M4: "前端可基于正式合同及明确标记为 synthetic 的样本并行开发").
 * Not covered: any real data, scoring or market logic.
 */
import type { Coverage, QualityStatus, FreshnessState, ResponseMeta, Warning } from '../src/api/types';

export function fnv(str: string, seed = 2166136261): number {
  let h = seed >>> 0;
  for (let i = 0; i < str.length; i += 1) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}

export function hex(str: string, len: number): string {
  let out = '';
  let seed = 2166136261;
  while (out.length < len) {
    seed = fnv(str + out.length, seed);
    out += seed.toString(16).padStart(8, '0');
  }
  return out.slice(0, len);
}

/** Deterministic UUID-shaped id (contract pattern only requires lower-hex 8-4-4-4-12). */
export function uuid(name: string): string {
  const h = hex(`uuid:${name}`, 32);
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
}

/** Contract 'seed-extension-point' objects are generated as Record<string, never>; this marks intentional opaque payloads. */
export function opaque(v: object): never {
  return v as never;
}

export const hash64 = (name: string): string => hex(`hash:${name}`, 64);

export function rng(seed: string): () => number {
  let s = fnv(seed) || 1;
  return () => {
    s ^= s << 13; s >>>= 0;
    s ^= s >>> 17;
    s ^= s << 5; s >>>= 0;
    return (s >>> 0) / 4294967296;
  };
}

export const iso = (ms: number): string => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

export function dec(n: number, digits = 2): string {
  return n.toFixed(digits);
}

export const SYNTHETIC_WARNING: Warning = {
  code: 'SYNTHETIC_FIXTURE',
  message: '合成样本：所有记录、价格和时间都是虚构的测试值，不是真实行情或选股结果。',
  affected_component: null,
  evidence_ref: null,
};

export interface MetaOpts {
  releaseId?: string | null;
  decisionAsOf?: string | null;
  quality?: QualityStatus;
  freshness?: FreshnessState;
  coverage?: Partial<Coverage>;
  warnings?: Warning[];
  nextCursor?: string | null;
  actionabilityId?: string | null;
  snapshots?: string[];
}

const SNAPSHOT_DATASETS: Record<string, string> = {
  bars: 'syn.raw_bars',
  membership: 'syn.membership',
  quote: 'syn.live_quote',
  valuation: 'syn.valuation_marks',
};

export function buildMeta(now: number, o: MetaOpts): ResponseMeta {
  const snaps = o.snapshots ?? ['bars', 'membership'];
  return {
    request_id: crypto.randomUUID(),
    generated_at: iso(now),
    trace_id: hex(`trace:${now}:${Math.random()}`, 32),
    contract_version: '3.0.0',
    release_id: o.releaseId ?? null,
    decision_as_of: o.decisionAsOf ?? null,
    availability_policy_version: 'synthetic-observed-clock-v1',
    input_snapshot_refs: snaps.map((k) => ({
      snapshot_id: uuid(`snap:${k}`),
      dataset_id: SNAPSHOT_DATASETS[k] ?? `syn.${k}`,
      manifest_version: 1,
      content_hash: hash64(`snap:${k}`),
    })),
    quality_status: o.quality ?? 'valid',
    freshness_state: o.freshness ?? 'current',
    coverage: {
      policy_version: 'synthetic-coverage-v1',
      expected_count: 1,
      observed_count: 1,
      missing_count: 0,
      unverifiable_reasons: [],
      ...o.coverage,
    },
    warnings: [SYNTHETIC_WARNING, ...(o.warnings ?? [])],
    next_cursor: o.nextCursor ?? null,
    actionability_id: o.actionabilityId ?? null,
  };
}

export function encodeCursor(parts: Array<string | number>): string {
  return Buffer.from(parts.join('|'), 'utf-8').toString('base64url');
}

export function decodeCursor(cursor: string): string[] | null {
  try {
    const s = Buffer.from(cursor, 'base64url').toString('utf-8');
    return s.split('|');
  } catch {
    return null;
  }
}

export interface MockResponse {
  status: number;
  body: unknown;
}

export function errorResponse(now: number, status: number, code: string, message: string, fieldErrors: Array<{ path: string; code: string; message: string }> = [], retryable = false): MockResponse {
  return {
    status,
    body: {
      meta: buildMeta(now, { quality: 'invalid', freshness: 'unknown' }),
      error: { code, message, retryable, field_errors: fieldErrors },
    },
  };
}
