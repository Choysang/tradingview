/** Alert tier parsing/grouping. Tier and headline live in the evidence artifact body, not in AlertView. */
import type { AlertView } from './types';

export type Tier = 'P0' | 'P1' | 'P2' | 'RULE';
export type TierKey = Tier | 'unknown';
export const TIER_ORDER: TierKey[] = ['P0', 'P1', 'P2', 'RULE', 'unknown'];
/** Short label for filter buttons. */
export const TIER_SHORT: Record<TierKey, string> = { P0: 'P0', P1: 'P1', P2: 'P2', RULE: '规则', unknown: '未分级' };
export const TIER_LABEL: Record<TierKey, string> = {
  P0: 'P0 持仓负面', P1: 'P1 持仓所在行业的政策负面', P2: 'P2 自选或正面', RULE: '规则（你对个股/行业设置的规则）', unknown: '未分级',
};

export interface AlertEvidence {
  tier: TierKey;
  headline: string;
  category: string | null;
  direction: 'negative' | 'positive' | null;
  instruments: Array<{ instrument_id: string; code?: string; name?: string }>;
  industries: unknown[];
  reason: string | null;
  verification_note: string | null;
  held_in_industry: unknown;
  claim_status: string | null;
  source_claim_revision_ids: string[];
  already_reacted: Record<string, unknown> | null;
  rule_id: string | null;
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === 'string' && v.length > 0 ? v : null);

export function parseTier(v: unknown): TierKey {
  return v === 'P0' || v === 'P1' || v === 'P2' || v === 'RULE' ? v : 'unknown';
}

/** Tolerant parse of an evidence artifact body; null when it is not an object. */
export function parseEvidence(body: unknown): AlertEvidence | null {
  if (!isObj(body)) return null;
  const inst = Array.isArray(body.instruments) ? body.instruments.filter(isObj).map((i) => ({ instrument_id: String(i.instrument_id ?? ''), code: str(i.code) ?? undefined, name: str(i.name) ?? undefined })) : [];
  return {
    tier: parseTier(body.tier),
    headline: str(body.headline) ?? '（无标题）',
    category: str(body.category),
    direction: body.direction === 'negative' || body.direction === 'positive' ? body.direction : null,
    instruments: inst,
    industries: Array.isArray(body.industries) ? body.industries : [],
    reason: str(body.reason),
    verification_note: str(body.verification_note),
    held_in_industry: body.held_in_industry ?? null,
    claim_status: str(body.claim_status),
    source_claim_revision_ids: Array.isArray(body.source_claim_revision_ids) ? body.source_claim_revision_ids.map(String) : [],
    already_reacted: isObj(body.already_reacted) ? body.already_reacted : null,
    rule_id: str(body.rule_id),
  };
}

export interface AlertItem { alert: AlertView; evidence: AlertEvidence | null }

/** Evidence not loaded yet (or unreadable) counts as 未分级; order inside a tier is newest first. */
export function groupAlertsByTier(items: readonly AlertItem[]): Array<{ tier: TierKey; items: AlertItem[] }> {
  const by = new Map<TierKey, AlertItem[]>();
  for (const it of items) {
    const t = it.evidence?.tier ?? 'unknown';
    by.set(t, [...(by.get(t) ?? []), it]);
  }
  return TIER_ORDER.filter((t) => by.has(t)).map((tier) => ({
    tier,
    items: [...(by.get(tier) ?? [])].sort((a, b) => b.alert.raised_at.localeCompare(a.alert.raised_at)),
  }));
}

export type Channel = 'in_app' | 'webhook' | 'unknown';
export const DELIVERY_LABEL: Record<string, string> = {
  pending: '待发送', sending: '发送中', accepted: '对方已接收', confirmed: '已确认', failed: '失败', unknown: '结果未知', expired: '已过期',
};
export const CHANNEL_LABEL: Record<Channel, string> = { in_app: '站内', webhook: 'Webhook', unknown: '通道未标明' };

/**
 * delivery_states entries are plain strings; the channel is optional. Accepted shapes: "confirmed", "in_app:confirmed",
 * "webhook=failed", "webhook/accepted". Anything else keeps the raw text as the state.
 */
export function parseDelivery(s: string): { channel: Channel; state: string } {
  const m = /^(in_app|webhook)[:=/ ]\s*(\w+)$/.exec(s.trim());
  if (m) return { channel: m[1] as Channel, state: m[2] as string };
  return { channel: 'unknown', state: s.trim() };
}
