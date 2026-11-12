/**
 * Client configuration for values the v3.0.0 contract needs as inputs but does not
 * enumerate (horizon ids, taxonomy ids, slot key, account/league pointers).
 * Why: the contract has no "list horizons/taxonomies/accounts/leagues" operation, so
 * these are deployment config with env overrides. They are NOT business data and
 * never hold user state. Unresolved items are listed in docs/web/M4_evidence.md.
 */

/**
 * Values served by the API at /app-config.json (derived from the published release; see README). main.tsx fetches
 * them once and stores them on window before this module is evaluated. Precedence: VITE_* env > served config > default.
 */
interface RuntimeConfig {
  slot_key?: string;
  horizons?: HorizonConfig[];
  taxonomies?: TaxonomyConfig[];
  quote_policy_version?: string;
  session_policy_version?: string;
}
declare global {
  interface Window { __PULSE_CONFIG__?: RuntimeConfig }
}
const runtime: RuntimeConfig = (typeof window !== 'undefined' ? window.__PULSE_CONFIG__ : undefined) ?? {};

function envString(key: string): string | undefined {
  const v = (import.meta.env as Record<string, string | undefined>)[key];
  return v && v.length > 0 ? v : undefined;
}

function envJson<T>(key: string, fallback: T): T {
  const raw = envString(key);
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export interface HorizonConfig {
  key: 'short' | 'swing' | 'long';
  label: string;
  /** Plain-language description of what the ranking target means. */
  hint: string;
  horizonId: string;
  forecastTarget: string;
}

export interface TaxonomyConfig {
  id: string;
  version: number;
  label: string;
}

export const USE_MOCK = envString('VITE_USE_MOCK') === '1';
export const SLOT_KEY = envString('VITE_SLOT_KEY') ?? runtime.slot_key ?? 'main';
export const API_ORIGIN = envString('VITE_API_ORIGIN') ?? '';

export const HORIZONS: HorizonConfig[] = envJson<HorizonConfig[]>('VITE_HORIZONS', runtime.horizons ?? [
  { key: 'short', label: '短线', hint: '未来约 5 个交易日的相对机会', horizonId: 'short_term', forecastTarget: 'excess_return_5d' },
  { key: 'swing', label: '波段', hint: '未来约 20 个交易日的相对机会', horizonId: 'swing', forecastTarget: 'excess_return_20d' },
  { key: 'long', label: '长期', hint: '未来约 120 个交易日的相对机会', horizonId: 'long_term', forecastTarget: 'excess_return_120d' },
]);

export const TAXONOMIES: TaxonomyConfig[] = envJson<TaxonomyConfig[]>('VITE_TAXONOMIES', runtime.taxonomies ?? [
  { id: 'industry', version: 1, label: '行业' },
  { id: 'concept', version: 1, label: '概念题材' },
]);

export const PAGE_SIZE_HINT = 20;

export interface AccountContext {
  accountId: string | null;
  accountVersion: number | null;
  valuationSnapshotId: string | null;
}

export const ACCOUNT_DEFAULTS: AccountContext = {
  accountId: envString('VITE_ACCOUNT_ID') ?? null,
  accountVersion: envString('VITE_ACCOUNT_VERSION') ? Number(envString('VITE_ACCOUNT_VERSION')) : null,
  valuationSnapshotId: envString('VITE_VALUATION_SNAPSHOT_ID') ?? null,
};

/** Fixed per-installation layout id (the layout itself is stored through the API). */
export const LAYOUT_ID = envString('VITE_LAYOUT_ID') ?? '5a0d3c1e-7b2f-4c60-9a11-0000000000a1';
export const LEAGUE_DEFAULT = {
  id: envString('VITE_LEAGUE_ID') ?? null,
  version: envString('VITE_LEAGUE_VERSION') ? Number(envString('VITE_LEAGUE_VERSION')) : 1,
};
export const QUOTE_POLICY_VERSION = envString('VITE_QUOTE_POLICY_VERSION') ?? runtime.quote_policy_version ?? null;
/**
 * Used only when the workspace does not carry a session/adjustment version. Real mode sends no adjustment_version
 * unless the frozen feature key declares one: the server fills in the snapshot's own version or answers 409.
 */
export const DEFAULT_SESSION_POLICY_VERSION = envString('VITE_SESSION_POLICY_VERSION') ?? runtime.session_policy_version ?? 'regular_continuous_v1';
export const DEFAULT_ADJUSTMENT_VERSION: string | undefined = envString('VITE_ADJUSTMENT_VERSION') ?? (USE_MOCK ? 'adj-v1' : undefined);

export function horizonByKey(key: string | null | undefined): HorizonConfig {
  return HORIZONS.find((h) => h.key === key) ?? (HORIZONS[0] as HorizonConfig);
}
