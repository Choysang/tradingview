/**
 * Watchlist cards for the home page (triage #38/#39): last close, change, sparkline, amount (estimate) and the instrument's
 * current rank in the release. All figures come from the board lane watch-cards read pinned to the release; a missing value
 * is shown as missing with its reason. When the watchlist is empty the page offers the top 8 of the release's market ranking.
 */
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ops } from '../../../api/ops';
import { Badge } from '../../../components/Badges';
import { InstrumentName, WatchStar, stockHref } from '../../../components/Lists';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { horizonByKey } from '../../../config';
import { boardOps, type PctMissingReason, type WatchCardData } from './api';
import { fmtPct, fmtYi } from './fmt';
import './board.css';

const SUGGEST_COUNT = 8;
const MISSING_TEXT: Record<PctMissingReason, string> = { no_bar: '该发布没有这只股票的日线', no_previous_bar: '缺少前一日收盘，无法算涨跌幅', unverifiable_move: '涨跌幅超出合理范围（可能除权），不显示' };

/** SVG path of the close series scaled into the box; fewer than 2 points draws nothing (never a flat line for missing data). */
export function sparkPath(values: ReadonlyArray<string>, w = 120, h = 36): string | null {
  const nums = values.map(Number).filter((n) => Number.isFinite(n));
  if (nums.length < 2) return null;
  const lo = Math.min(...nums);
  const hi = Math.max(...nums);
  const x = (i: number): number => 2 + (i / (nums.length - 1)) * (w - 4);
  const y = (v: number): number => h - 3 - ((v - lo) / Math.max(hi - lo, 1e-9)) * (h - 6);
  return nums.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
}

function Card({ c, releaseId, horizonKey, label }: { c: WatchCardData; releaseId: string; horizonKey: string; label: string }) {
  const pct = c.pct_change == null ? null : Number(c.pct_change);
  const tone = pct == null ? '' : pct > 0 ? 'bd-up' : pct < 0 ? 'bd-down' : '';
  const path = sparkPath(c.spark.map((p) => p.c));
  return (
    <li className="card bd-card" aria-label={`自选卡片 ${c.instrument_id.slice(0, 8)}`}>
      <div className="bd-card-head">
        <Link className="row-link grow" to={stockHref(c.instrument_id, releaseId, horizonKey)}><InstrumentName instrumentId={c.instrument_id} releaseId={releaseId} /></Link>
        <WatchStar instrumentId={c.instrument_id} releaseId={releaseId} />
      </div>
      <div className="bd-card-price">
        <b className={`mono ${tone}`}>{c.last_close ?? '—'}</b>
        {pct != null ? <span className={`mono ${tone}`}>{fmtPct(c.pct_change)}</span> : <span className="muted small" title={c.pct_missing_reason ? MISSING_TEXT[c.pct_missing_reason] : undefined}>涨跌幅缺失</span>}
      </div>
      {path ? <svg className={`bd-spark ${tone}`} viewBox="0 0 120 36" preserveAspectRatio="none" role="img" aria-label="近期收盘价走势"><path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>
        : <div className="muted small">走势数据不足</div>}
      <div className="bd-card-meta small">
        <span title="日线没有成交额字段，按 股数×收盘价 估算">成交额 {fmtYi(c.amount_estimate_cny)}<span className="muted">（估算）</span></span>
        {c.rank ? <span title={`${label}榜 ${c.rank.horizon_id}`}>第 <b className="mono">{c.rank.alpha_rank}</b> / {c.rank.total}</span> : <span className="muted">不在本期榜内</span>}
      </div>
      {c.session && <div className="muted small">收盘日 {c.session}</div>}
    </li>
  );
}

export function WatchCards({ releaseId, ids, suggested = false }: { releaseId: string; ids: readonly string[]; suggested?: boolean }) {
  const h = horizonByKey(null);
  const key = ids.join(',');
  const q = useQuery({
    queryKey: ['bf-board', 'watch-cards', releaseId, h.horizonId, h.forecastTarget, key],
    queryFn: ({ signal }) => boardOps.watchCards({ release_id: releaseId, ids: [...ids], horizon_id: h.horizonId, forecast_target: h.forecastTarget }, signal),
    enabled: ids.length > 0,
    staleTime: 30_000,
  });
  if (ids.length === 0) return null;
  if (q.isLoading) return <Loading label="读取自选行情…" />;
  if (q.error || !q.data) return <ErrorState error={q.error ?? new Error('没有返回')} what="自选卡片" onRetry={() => void q.refetch()} />;
  const byId = new Map(q.data.data.cards.map((c) => [c.instrument_id, c]));
  return (
    <>
      <p className="muted small">行情取自当前发布版本的收盘日线{suggested ? '；自选为空，下面是本期排名前 8，点 ☆ 加入自选。' : '。'}</p>
      <ul className="bd-cards" aria-label={suggested ? '本期排名前 8' : '自选行情卡片'}>
        {ids.map((id) => {
          const c = byId.get(id);
          return c ? <Card key={id} c={c} releaseId={releaseId} horizonKey={h.key} label={h.label} /> : <li key={id} className="card bd-card muted small">没有返回这只股票的行情。</li>;
        })}
      </ul>
    </>
  );
}

/** Empty watchlist: the top 8 of the release's market ranking (first page of the existing ranking read) as cards. */
export function SuggestedCards({ releaseId }: { releaseId: string }) {
  const h = horizonByKey(null);
  const q = useQuery({
    queryKey: ['bf-board', 'top8', releaseId, h.horizonId, h.forecastTarget],
    queryFn: ({ signal }) => ops.getMarketRanking({ release_id: releaseId, horizon_id: h.horizonId, forecast_target: h.forecastTarget }, { signal }),
    staleTime: 30_000,
  });
  if (q.isLoading) return <Loading label="读取本期排名…" />;
  if (q.error) return <ErrorState error={q.error} what="本期排名" onRetry={() => void q.refetch()} />;
  const top = q.data?.data.entries.slice(0, SUGGEST_COUNT).map((e) => e.instrument_id) ?? [];
  if (top.length === 0) return <EmptyState>本期榜单为空，没有可推荐的股票。</EmptyState>;
  return (
    <>
      <Badge tone="neutral">本期{h.label}排名前 {top.length}</Badge>
      <WatchCards releaseId={releaseId} ids={top} suggested />
    </>
  );
}
