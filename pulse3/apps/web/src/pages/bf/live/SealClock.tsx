/**
 * Seal clock (LEGACY_TRIAGE #5/#124): histogram of FIRST-seal times plus the cumulative curve of one session, and the per-stock seal facts.
 * Every number is computed by the server from the 60-second recorded snapshots and is an estimate; this file only draws and formats.
 * A session without published facts shows an explicit empty state, never a zero chart.
 */
import { ApiError } from '../../../api/client';
import { Badge } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { fmtInt } from '../../../lib/format';
import './bf-live.css';
import { useSealClock, useSealFacts } from './api';
import type { SealClockData, SealFact } from './types';

const W = 640;
const H = 190;
const PAD = { l: 28, r: 8, t: 10, b: 24 };

/** `HH:MM` Asia/Shanghai of a UTC instant. */
export function cstClock(z: string | null): string {
  if (!z) return '—';
  return new Date(z).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit' });
}

export function wanYuan(v: string | null): string {
  if (v == null) return '—';
  const n = Number(v);
  return n >= 1e8 ? `${(n / 1e8).toFixed(2)} 亿` : `${(n / 1e4).toFixed(0)} 万`;
}

export function SealClockChart({ data }: { data: SealClockData }) {
  const n = data.buckets.length;
  const max = Math.max(...data.buckets.map((b) => b.first_seal_count), 1);
  const iw = W - PAD.l - PAD.r;
  const ih = H - PAD.t - PAD.b;
  const slot = iw / Math.max(n, 1);
  const x = (i: number) => PAD.l + i * slot + slot / 2;
  const yc = (r: number) => PAD.t + ih - r * ih;
  let d = '';
  let open = false;
  data.buckets.forEach((b, i) => {
    if (b.cumulative_ratio == null) { open = false; return; }
    d += `${open ? 'L' : 'M'}${x(i).toFixed(1)},${yc(Number(b.cumulative_ratio)).toFixed(1)} `;
    open = true;
  });
  const every = Math.ceil(n / 12);
  return (
    <svg className="bl-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`封板时钟：首次封板时间分布与累计曲线，${data.session_date}，估算`} preserveAspectRatio="none">
      <line className="bl-axis" x1={PAD.l} x2={W - PAD.r} y1={PAD.t + ih} y2={PAD.t + ih} />
      {data.buckets.map((b, i) => {
        const h = (b.first_seal_count / max) * ih;
        return (
          <g key={b.clock}>
            <rect className="bl-bar" x={x(i) - slot * 0.4} y={PAD.t + ih - h} width={slot * 0.8} height={h}>
              <title>{`${b.clock} 起 ${data.bucket_minutes} 分钟：首次封板 ${b.first_seal_count} 只，累计 ${b.cumulative_count} 只`}</title>
            </rect>
            {i % every === 0 && <text className="bl-tick" x={x(i)} y={H - 8} textAnchor="middle">{b.clock}</text>}
          </g>
        );
      })}
      {d && <path className="bl-curve" d={d} />}
    </svg>
  );
}

export function SealClockSummary({ data }: { data: SealClockData }) {
  return (
    <>
      <div className="bl-summary small">
        <span>会话 <b className="mono">{data.session_date}</b></span>
        <span>触及涨停 <b className="mono">{fmtInt(data.touched_limit_count)}</b></span>
        <span>封住过 <b className="mono">{fmtInt(data.sealed_ever_count)}</b></span>
        <span>收盘仍封住 <b className="mono">{fmtInt(data.sealed_at_close_count)}</b></span>
        <span title="两次快照之间炸板又回封看不到，所以是下限">炸板（下限）<b className="mono">{fmtInt(data.broken_count_total)}</b></span>
      </div>
      <SealClockChart data={data} />
      <p className="muted small">柱：该时段首次封板的家数；线：累计占封住过家数的比例。时间为北京时间，由约每分钟一次的快照估算，可能比真实封板晚至多一个采样间隔。</p>
      <details className="small">
        <summary>口径与来源</summary>
        <ul>{data.notes.map((t) => <li key={t}>{t}</li>)}</ul>
        <p className="muted">版本 {data.policy_version}；数据快照 <span className="mono">{data.source.snapshot_id.slice(0, 8)}</span>，入库时间 {data.source.available_at ?? '—'}。</p>
      </details>
    </>
  );
}

export function SealClockCard({ sessionDate, missingNote, releaseId = null }: { sessionDate?: string | null; missingNote?: string; releaseId?: string | null }) {
  const q = useSealClock(sessionDate, releaseId);
  const missing = q.error instanceof ApiError && q.error.status === 404;
  return (
    <section className="card" aria-label="封板时钟">
      <div className="ea-head"><h3>封板时钟</h3><Badge tone="warn" title="由约 60 秒一次的快照估算，不是逐笔数据">估算</Badge></div>
      {q.isLoading && <Loading />}
      {missing && <EmptyState>{missingNote ?? '该交易日还没有封板事实'}。盘中记录导入后才会显示封板时间分布，这里不编造。</EmptyState>}
      {q.error && !missing && <ErrorState error={q.error} what="封板时钟" onRetry={() => void q.refetch()} />}
      {q.data && <SealClockSummary data={q.data.data} />}
    </section>
  );
}

export function SealFactRows({ fact }: { fact: SealFact }) {
  const first = fact.first_seal_at
    ? `${cstClock(fact.first_seal_at)}${fact.first_seal_uncertainty_seconds != null ? `（可能早至 ${Math.round(fact.first_seal_uncertainty_seconds / 60)} 分钟前）` : ''}`
    : fact.touched_limit ? '触及涨停但未封住' : '未触及涨停';
  return (
    <dl className="bl-facts small">
      <dt>首次封板</dt><dd className="mono">{first}</dd>
      <dt>最后封板</dt><dd className="mono">{cstClock(fact.last_seal_at)}</dd>
      <dt>炸板（下限）</dt><dd className="mono">{fact.broken_count}</dd>
      <dt>收盘封住</dt><dd>{fact.sealed_at_close == null ? '未观察到收盘快照' : fact.sealed_at_close ? '是' : '否'}</dd>
      <dt>收盘封单额</dt><dd className="mono">{wanYuan(fact.close_seal_order_amount_cny)}</dd>
    </dl>
  );
}

/** Stock-page panel: the StockPage owner mounts it (`<SealFactsPanel instrumentId=... />`); it reads GET /v1/x/bf/live/seal-facts. */
export function SealFactsPanel({ instrumentId, sessionDate, releaseId = null }: { instrumentId: string | undefined; sessionDate?: string | null; releaseId?: string | null }) {
  const q = useSealFacts(instrumentId, sessionDate, releaseId);
  return (
    <section className="card" aria-label="封板事实">
      <div className="ea-head"><h3>封板事实</h3><Badge tone="warn" title="由约 60 秒一次的快照估算">估算</Badge>{q.data?.data.session_date && <span className="muted small">{q.data.data.session_date}</span>}</div>
      {q.isLoading && <Loading />}
      {q.error && <ErrorState error={q.error} what="封板事实" onRetry={() => void q.refetch()} />}
      {q.data?.data.status === 'ok' && q.data.data.fact && <SealFactRows fact={q.data.data.fact} />}
      {q.data?.data.status === 'not_observed' && <EmptyState>{q.data.data.notes[0] ?? '该证券当日没有快照'}</EmptyState>}
      {q.data?.data.status === 'no_dataset' && <EmptyState>该交易日还没有封板事实（盘中记录尚未导入）。</EmptyState>}
    </section>
  );
}
