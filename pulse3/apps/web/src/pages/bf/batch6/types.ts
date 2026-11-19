/** Wire types of the batch6 lane (src/pulse/contracts/data/backfill_batch6.schema.json). Every experiment is a SHADOW record: nothing is booked. */
import type { Resp } from '../../ext/a/types';

export type { Resp };
export interface HistoryRow {
  hypothesis: string; experiment: string; segment: 'PRE' | 'DEV'; sign: number; hold: number; signals: number; attempted: number; filled: number; fill_rate: string | null;
  matched: number; signal_days: number; mean_ret_net: string | null; mean_baseline: string | null; excess: string | null; p_value: string | null; holm_p_adj: string | null;
  blocks: number; reliable: boolean; note: string | null;
}
export interface Verdict { hypothesis: string; passed: boolean; reason: string }
export interface Count { key: string; count: number }
export interface HistoryBlock {
  as_of: string | null; decision_days: { first: string; last: string; sessions: number } | null; rows: HistoryRow[]; verdicts: Verdict[]; event_counts: Count[]; notes: string[];
}
export interface ForwardRow {
  experiment: string; hypothesis: string; sign: number; days_evaluated: number; signal_days: number; experiment_signal_days: number; target_days: number; signals: number;
  filled: number; exit_deferred: number; resolved_later: number; fill_rate: string | null; mean_ret_net: string | null; excess: string | null; p_value: string | null;
  holm_p_adj: string | null; state: 'collecting' | 'descriptive' | 'judged'; passed: boolean | null; reason: string | null;
}
export interface NamedShare { name: string; value: string | null }
export interface FieldCheck {
  sessions: string[]; unmatched_buy_computable: boolean; bid1_ask1_lots_0915_0924: string; bid2_ask2_lots_0915_0924: string; bid_ask_book_at_0925: string;
  unmatched_buy_definition: string; auction_rows: number; final_rows: number; shares: NamedShare[];
}
export interface Overview {
  spec_sha256: string; first_evaluated: string; history: HistoryBlock | null; forward: ForwardRow[]; fieldcheck: FieldCheck | null; sessions_decided: string[];
}
