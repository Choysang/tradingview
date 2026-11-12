/**
 * Wire shapes for the extension-C operations (src/pulse/contracts/data/extension.schema.json)
 * and the read-only support routes under /v1/x/c/*. The generated client only covers the 31 core
 * operations, so these are hand-written mirrors; keep them in sync with the contract.
 */
import type { Condition, Coverage, JobReceipt, JobRun, ResponseMeta, Schemas, SeatReport } from '../../../api/types';

export type ArtifactRef = Schemas['ArtifactRef'];
export type { Condition, Coverage, JobReceipt, JobRun, ResponseMeta, SeatReport };

export interface Window { start: string; end: string }
export interface PageInput { limit: number; cursor?: string | null }
export interface PageToken { next_cursor: string | null; snapshot_hash: string }
export interface Metric { name: string; definition_version: string; value: string | null; unit: string; sample_count: number; missing_reason: string | null }
export interface Measurement { value: string | null; text_value: string | null; unit: string | null; reference_period: string; source_revision_id: string }

export interface AlertView {
  alert_id: string; rule_id: string; rule_version: number; raised_at: string;
  evidence_artifact: ArtifactRef; delivery_states: string[]; outcome_artifact: ArtifactRef | null;
}
export interface ListUserAlertsData { alerts: AlertView[]; page: PageToken }

export interface AlertScope { scope_kind: string; scope_key: string; membership_snapshot_id: string | null }
export interface AlertRuleBody {
  scope: AlertScope; condition: Condition; policy_version: string;
  annotation_id: string | null; annotation_version: number | null; destination_ids: string[]; enabled: boolean;
}
export interface AlertRuleEntry { rule_id: string; rule_version: number; updated_at: string; body: AlertRuleBody }
export interface AlertDestination { destination_id: string; channel: 'in_app' | 'webhook'; enabled: boolean; endpoint: 'configured' | 'not_configured'; label: string }
export interface AlertRulesData { rules: AlertRuleEntry[]; destinations: AlertDestination[]; webhook: { configured: boolean; kind: string | null; host: string | null } }

export interface CalendarRevision {
  calendar_event_id: string; revision: number; scheduled_at: string | null; scheduled_date: string | null;
  time_precision: string; status: string; expected: Measurement | null; actual: Measurement | null; available_at: string;
}
export interface CalendarEventsData { events: CalendarRevision[]; page: PageToken }
export interface CalendarLinksData {
  links: Array<{ industry_node_id: string; industry_name: string; link_rule_version: string | number; instruments: Array<{ instrument_id: string; code: string; name: string }> }>;
  decision_as_of: string;
}

export interface SeatDirectoryItem {
  seat_id: string | null; seat_version: number | null; display_name: string; resolution_status: string;
  report_local_ref: { report_revision_id: string; side: string; rank: number } | null;
}
export interface SeatDirectoryData { seats: SeatDirectoryItem[]; page: PageToken }
export interface SeatReportsData { reports: SeatReport[]; page: PageToken }
export interface SeatProfileData { profile: ArtifactRef; sample_count: number; limitations: string[] }
export interface SeatProfileBody { metrics?: Metric[]; sample_count?: number; limitations?: string[]; policy_version?: string }

export interface AccountListEntry { account_id: string; account_version: number; base_currency: string }
export interface AccountsData { accounts: AccountListEntry[]; page: PageToken }
export interface OrderView {
  order_id: string; instrument_id: string; side: string; state: string; original_quantity: number;
  cumulative_filled_quantity: number; leaves_quantity: number; limit_price_raw: string | null;
  account_event_ids: string[]; state_observed_at: string; quality_status: string;
}
export interface OrdersData { orders: OrderView[]; coverage: Coverage; page: PageToken }
export interface AccountPerformance {
  account_id: string; account_version: number; window: Window; performance_policy_version: string; valuation_policy_version: string;
  metrics: Metric[]; coverage: Coverage; artifact: ArtifactRef;
}
export interface PerformanceData { performance: AccountPerformance }
export interface OpportunitiesData { opportunity_artifact: ArtifactRef; page: PageToken }

export interface ArtifactData { ref: string; content_hash: string; contract_id: string; schema_version: string; body: Record<string, unknown> }

export interface ForwardBookRule { hold_days: number | string; slots: number | string; new_per_day: number | string; offer_top: number | string; per_name_cny: number | string }
export interface ForwardBookHolding { instrument_id: string; code: string | null; name: string | null; quantity: number; cost_cny: string | null; mark_price: string | null }
export interface ForwardBook {
  account_id: string; book_key: 'main_h5' | 'shadow_h10'; label: string; status_label: string; tracking_kind: string; ranking_weight: string;
  rule: ForwardBookRule; account_version: number; base_currency: string; frozen_on: string;
  latest: null | {
    trade_date: string; release_id: string; nav_cny: string | null; cash_cny: string | null; holdings: ForwardBookHolding[];
    planned_buys: Array<{ instrument_id: string; code: string; name: string; alpha_rank: number }>; missing_marks: number;
  };
  history: Array<{ trade_date: string; nav_cny: string | null; positions: number }>;
  notes: string[];
}
export interface ForwardBooksData { books: ForwardBook[] }

export interface DatasetStatus {
  dataset_id: string; snapshot_id: string | null; manifest_version: number | null; manifest_state: string;
  ready_snapshot: unknown | null; row_count: number | null; available_watermark: string | null; coverage: Coverage;
  issues: Array<{ dataset_id: string; requirement: string; reason_code: string }>;
}
export interface DatasetStatusData { datasets: DatasetStatus[]; page: PageToken }
export interface JobRunsData { jobs: JobRun[]; page: PageToken }
export interface RunJobData { receipt: JobReceipt }

export interface CalendarName { name: string; category: string | null; region: string | null; source_id: string | null }
export interface CalendarNamesData { names: Record<string, CalendarName> }

export interface JobDefinition {
  job_id: string; job_version: number; application_operation: string; manual_trigger: boolean; enabled: boolean;
  definition_hash: string; parameter_contract_id: string | null; parameter_schema_version: string | null;
}
export interface JobDefinitionsData { definitions: JobDefinition[] }

export interface SnapshotRef { snapshot_id: string; dataset_id: string; manifest_version: number; content_hash: string }
export interface ReadModelContext {
  projection_kind: 'orders' | 'performance'; account_version: number; window: Window; knowledge_as_of: string; valuation_snapshot_refs: SnapshotRef[];
}
export interface ReadModelContextData { contexts: ReadModelContext[] }

export interface OpportunityItem {
  opportunity_id: string; opportunity_version: number; instrument_id: string; side: string; scenario_id: string; state: string;
  target_quantity: number | string | null; filled_quantity: number | string | null; reason_code: string | null; account_event_ids: string[]; available_at: string;
}
export interface IncrementalVsHold {
  status: 'ok' | 'no_trades' | 'mark_missing' | 'not_comparable';
  incremental_pnl_cny: string | null; active_fees_cny: string | null; baseline_fees_cny: string | null;
  inventory_shortfall: Record<string, number | string> | null; inventory_value_delta_cny: string | null; bridge_holds: unknown;
}
export interface OpportunitiesBody {
  as_of?: string; account_id?: string; experiment_id?: string | null; opportunities?: OpportunityItem[];
  denominators?: { total?: number; by_state?: Record<string, number> };
  incremental_vs_hold?: IncrementalVsHold | null;
}
