/**
 * Types of the extension-A operations, derived 1:1 from src/pulse/contracts/data/extension.schema.json
 * (queryMarketToday, queryMarketSentiment, querySectorDetails, queryRankingHistory, queryRankingTrackRecord,
 * streamRegisteredUpdates) plus the artifact bodies those responses point to.
 *
 * Why hand-written: openapi-typescript cannot resolve the `urn:pulse:contracts:ext:v1#/$defs/...` references of
 * extension.openapi.json, so the wire shapes are restated here and the v3 types they reference are imported from
 * the generated `api/schema.d.ts`. A drift check lives in tests/ext_a_types.test.ts (field lists vs. the schema).
 * Artifact bodies are internal, versioned JSON documents (market_today_service / domain.market_state); decimals
 * and dates arrive as strings and are only formatted, never recomputed here.
 */
import type { components } from '../../../api/schema';

type S = components['schemas'];

export type ResponseMeta = S['ResponseMeta'];
export interface Resp<T> { meta: ResponseMeta; data: T }

export interface Scope { scope_kind: string; scope_key: string; membership_snapshot_id: string | null }
export interface TypedFeature { feature_id: string; definition_version: string; key_hash: string; result: S['FeatureResult'] }
export interface MarketOverlay { kind: string; as_of: string; artifact: S['ArtifactRef']; scope: Scope }
export interface CapabilityGap { dataset_id: string; requirement: string; reason_code: string }
export interface Metric {
  name: string; definition_version: string; value: string | null; unit: string; sample_count: number; missing_reason: string | null;
}

export interface QueryMarketTodayData { release: S['AnalysisRelease']; state: TypedFeature[]; overlays: MarketOverlay[]; capability_gaps: CapabilityGap[] }
export interface QueryMarketSentimentData { features: TypedFeature[]; coverage: S['Coverage']; artifact: S['ArtifactRef'] }
export interface SectorMember { instrument_id: string; role_feature: TypedFeature | null }
export interface QuerySectorDetailsData {
  sector: S['TaxonomyNodeRef']; members: SectorMember[]; ranking: S['Ranking']; candidate_pools: S['CandidatePool'][];
  related_event_artifacts: S['ArtifactRef'][];
}
export interface RankingHistoryEntry { ranking_id: string; release_id: string; decision_as_of: string; outcome_artifact: S['ArtifactRef'] | null }
export interface PageToken { next_cursor: string | null; snapshot_hash: string }
export interface QueryRankingHistoryData { entries: RankingHistoryEntry[]; page: PageToken }
export interface QueryRankingTrackRecordData { outcomes: S['ArtifactRef'][]; pending_count: number }
export interface StreamEnvelope { cursor: string; scope: Scope; notification: { event_id: string; event_type: string; payload: Record<string, unknown> }; release_id: string | null }

/** `GET /v1/x/a/artifact` */
export interface ArtifactBody<T> { ref: string; feature_id: string; content_hash: string; body: T }

// ---- artifact bodies (pulse.market_today / pulse.market_sentiment / pulse.ranking_outcome) -------------------------
export interface MoodBand { index: number; word: string; hint: string; lower: number; upper: number }
export interface Thermometer {
  session: string; score: string | null; band: MoodBand | null; version: string; calibrated: boolean; weights: Record<string, string>;
  components: Record<string, string | null>; missing_components: string[]; bands: Array<{ upper: number; word: string; hint: string }>;
  history: Array<{ date: string; score: string | null }>; trend_delta_5d: string | null; trend_word: string | null;
}
export interface Coverage4 {
  expected_instruments: number | null; observed: number; pct_valid: number; pct_unverifiable: number; limit_verified: number;
  limit_unverifiable: number; missing: number | null;
}
export interface Who { instrument_id: string; code: string | null; name: string }
export interface Vitals {
  session: string; coverage: Coverage4; limit_up: number | null; broken: number | null; limit_down: number | null; seal_rate_pct: string | null;
  max_height: number | null; max_height_stocks: Who[];
  premium: { close_pct: string | null; open_pct: string | null; sample_count: number; definition: string };
  up: number; down: number; flat: number; mean_pct: string | null;
  volume: { shares: number | null; instruments: number; ratio_vs_20d_mean: string | null; amount_estimate_cny: string | null; amount_is_estimate: boolean; amount_method: string };
}
export interface LadderRung {
  streak: number; label: string; sealed_count: number; broken_count: number;
  promotion: { from_streak: number; frozen_count: number; promoted: number; missing: number; rate_pct: string | null } | null;
  sealed: Array<Who & { pct: string | null }>; broken: Array<Who & { pct: string | null }>;
}
export interface Ladder { session: string; max_height: number; rungs: LadderRung[]; note: string }
export interface Breadth {
  session: string; bins: Array<{ label: string; count: number }>; up: number; down: number; flat: number; pct_valid: number; pct_unverifiable: number;
  above_ma20: { count: number; of: number; pct: string | null };
}
export interface Themes {
  session: string; version: string; calibrated: boolean; formula: string; ranked: number;
  themes: Array<{ node_id: string; name: string; members: number; observed: number; limit_up: number; growth_streak_ge3: number; avg_pct: string; strength: string; leader: Who }>;
}
export interface GrowthStrip { session: string; ge3_total: number; definition: string; buckets: Array<{ streak: number; label: string; count: number; top: Array<Who & { growth_streak: number; pct: string | null }> }> }
export type RegimeDim = 'trend' | 'liquidity' | 'sentiment' | 'style';
export interface RegimePoint {
  date: string; index_close: string | null; trend: string; liquidity: string; sentiment: string; style: string; breadth_ma20: string | null;
  limit_up_count: number | null; broken_rate: string | null; max_height: number | null; amount_ratio: string | null; premium: string | null;
  lu_percentile: string | null; index_ma60: string | null; ma60_slope: string | null; style_spread: string | null;
  basis: Record<RegimeDim, string>;
}
export interface Regime {
  version: string; available: boolean; reason: string | null; index: { symbol: string; name: string; through: string; fallback: boolean } | null;
  stock_sessions_through?: string; points: RegimePoint[]; rules: string[]; labels?: Record<RegimeDim, Record<string, string>>;
  current: (RegimePoint & { label: string; explanation: string[] }) | null;
}
export interface TapeItem {
  event_id: string; recorded_at: string; event_kind: 'seal' | 'reseal' | 'break' | 'surge' | 'plunge' | 'new_high'; instrument_id: string;
  code: string | null; name: string; price: string; pct_chg: string; limit_streak: number | null; detail: string; themes: string[];
  trade_date: string; event_time: string | null; from_snapshot_id: string; to_snapshot_id: string; tape_version: string;
}
export interface TapePage { events: TapeItem[]; resume_cursor: string; reset_required: boolean; stream_epoch: string }
export interface OutcomeBody {
  ranking_id: string; release_id: string; horizon_id: string; horizon_sessions: number; decision_session: string; entry_session: string | null;
  exit_session: string | null; status: 'resolved' | 'pending'; record_kind?: 'live_forward' | 'reconstruction'; metrics: Metric[]; top_n: number;
  cost_ratio: string; cost_version: string; method_version: string; observed_through: string | null; unevaluable: Record<string, number>;
  members?: Array<{ instrument_id: string; alpha_rank: number; gross_return: string | null; reason: string | null }>;
}
