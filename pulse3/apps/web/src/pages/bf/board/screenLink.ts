/**
 * Links into the screen page (/screen) from other pages (triage #3, #8, #31).
 * URL parameters read by ScreenPage: `q` (a question parsed on arrival), `preset` (a preset id from the field catalog) and
 * `sector` + `tax` + `release` (keep only the members of that sector at that release in the shown result).
 * Why: the screen language has no sector or seal terms (`题材`, `炸板` are listed as unsupported), so a sector goes through
 * the members of the sector-details read and an unsupported tile gets no link instead of a question that cannot be parsed.
 */
import { useQuery } from '@tanstack/react-query';
import { currentReleaseQuery } from '../../../api/keys';
import { HORIZONS, TAXONOMIES } from '../../../config';
import { extOps } from '../../ext/a/api';

export interface ScreenLink { q: string | null; preset: string | null; sector: string | null; tax: string | null; release: string | null }

export function screenHref(o: { q?: string; preset?: string; sector?: string; tax?: string; release?: string | null }): string {
  const p = new URLSearchParams();
  if (o.q) p.set('q', o.q);
  if (o.preset) p.set('preset', o.preset);
  if (o.sector) p.set('sector', o.sector);
  if (o.sector && o.tax) p.set('tax', o.tax);
  if (o.release) p.set('release', o.release);
  const s = p.toString();
  return `/screen${s ? `?${s}` : ''}`;
}

export function parseScreenLink(sp: URLSearchParams): ScreenLink {
  const t = (k: string): string | null => { const v = sp.get(k)?.trim(); return v ? v : null; };
  return { q: t('q'), preset: t('preset'), sector: t('sector'), tax: t('tax'), release: t('release') };
}

/** The question to parse on arrival: an explicit `q` wins; otherwise the text of a known preset; otherwise none. */
export function linkText(link: ScreenLink, presets: ReadonlyArray<{ id: string; text: string }>): string | null {
  if (link.q) return link.q;
  return presets.find((p) => p.id === link.preset)?.text ?? null;
}

/** Tile -> question for the market vitals. A tile whose idea the screen language cannot express returns null (no link). */
export const VITAL_QUERY = {
  limitUp: '涨停',
  broken: null,
  limitDown: null,
  maxHeight: (n: number | null): string | null => (n != null && n >= 1 ? `${n}连板以上` : null),
  premium: null,
  amount: null,
  up: '上涨',
  down: '下跌',
} as const;

/** `N连阳以上` (the language has no exact-days form, so every bucket links to "at least N"). */
export function growthQuery(streak: number): string {
  return `${streak}连阳以上`;
}

export interface SectorMembers { ids: Set<string> | null; isLoading: boolean; error: unknown; name: string | null }

/** Members of the linked sector at the linked (or current) release, from the existing sector-details read. */
export function useSectorMembers(link: ScreenLink): SectorMembers {
  const current = useQuery({ ...currentReleaseQuery(), enabled: !!link.sector && !link.release });
  const rid = link.release ?? current.data?.data.current_release_id ?? null;
  const tax = TAXONOMIES.find((t) => t.id === link.tax) ?? (TAXONOMIES[0] as (typeof TAXONOMIES)[number]);
  const h = HORIZONS[0] as (typeof HORIZONS)[number];
  const q = useQuery({
    queryKey: ['bf-board', 'sector-members', rid, tax.id, tax.version, link.sector],
    queryFn: ({ signal }) => extOps.querySectorDetails({ sector: { taxonomy_id: tax.id, taxonomy_version: tax.version, node_id: link.sector as string }, release_id: rid as string, horizon_id: h.horizonId, forecast_target: h.forecastTarget }, signal),
    enabled: !!link.sector && !!rid,
    staleTime: 60_000,
  });
  if (!link.sector) return { ids: null, isLoading: false, error: null, name: null };
  const ids = q.data ? new Set(q.data.data.members.map((m) => m.instrument_id)) : null;
  return { ids, isLoading: current.isLoading || q.isLoading, error: q.error ?? current.error, name: link.sector };
}
