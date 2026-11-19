/** Wire types of the fwdlog lane (src/pulse/contracts/data/backfill_fwdlog.schema.json). Every experiment is a SHADOW record: nothing is booked. */
import type { Resp } from '../../ext/a/types';

export type { Resp };
export type ExperimentKey = 'E077' | 'E078' | 'E089' | 'PAPER';
export interface Preregistration { file: string; sha256: string }
export interface RegistryRef { registered: boolean; experiment_id: string | null; run_state: string | null }
export interface Progress { unit: string; done: number; target: number; remaining: number; share: string }
export interface Decision { check_at: number; check_unit: string; decided: boolean; verdict: string | null; pending_reason: string | null }
export interface Metric { name: string; value: string | number | boolean | null; provisional: boolean; pending_reason: string | null }
export interface PendingDay { session_date: string; reason: string }
export interface RuleRow {
  rule: string; triggered: number; mean_return_pct: string | null; mean_vs_close_pct: string | null; median_vs_close_pct: string | null;
  better_than_close_share: string | null; shadow: boolean;
}
export interface ExperimentCard {
  experiment_key: ExperimentKey; title: string; ledger_ids: string[]; purpose: 'shadow'; rules_version: string; preregistration: Preregistration[];
  registry: RegistryRef; status: 'not_started' | 'recording' | 'decision_reached'; first_session: string | null; last_session: string | null; sessions_decided: number;
  progress: Progress; decision: Decision; metrics: Metric[]; pending: PendingDay[]; rule_table: RuleRow[] | null; notes: string[];
}
export interface ExperimentsData { experiments: ExperimentCard[]; notes: string[] }
export interface DailyRow { session_date: string; row_key: string; decided_at: string; values: Record<string, unknown> }
export interface RowsData { experiment_key: ExperimentKey; dataset: string; total: number; rows: DailyRow[] }
