/**
 * Crowd and linkage panel for the market page (legacy rows #32 / #90 / #155): industry diffusion with leader, followers and a
 * per-session spread, institutional net buy from the Dragon-Tiger reports, and the name-cluster row, which is reported unavailable.
 * Every figure comes from `/v1/x/bf/fillm/crowd` pinned to the release; the page formats and never recomputes. A value pulse3
 * does not hold is shown as missing with its reason (never 0). The name-cluster row says why it is recommended for retirement.
 */
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { stockHref } from '../../../components/Lists';
import { fillm, type CrowdSnapshot, type CrowdStock, type DiffusionDay, type InstitutionRow } from './api';
import './fillm.css';

/** `3.5` -> `+3.50%`; null stays null (never 0). */
export function pctText(v: string | null | undefined): string | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? `${n > 0 ? '+' : ''}${n.toFixed(2)}%` : null;
}

/** CNY text in 亿元 with sign; null stays null. */
export function netText(v: string | null | undefined): string | null {
  if (v == null) return null;
  const n = Number(v);
  if (!Number.isFinite(n)) return null;
  return `${n > 0 ? '+' : ''}${(n / 1e8).toFixed(2)} 亿元`;
}

/** One cell of the spread: limit-ups and share of members up on that session; no figure when no member had a plausible move. */
export function spreadCell(d: DiffusionDay): string {
  const share = d.up_share == null ? '—' : `${Number(d.up_share).toFixed(0)}%`;
  return `${d.limit_ups} 板 · ${share} 上涨`;
}

function StockChip({ s, releaseId }: { s: CrowdStock; releaseId: string }) {
  const tone = s.pct_change == null ? '' : Number(s.pct_change) > 0 ? 'fm-up' : Number(s.pct_change) < 0 ? 'fm-down' : '';
  return (
    <Link className="fm-chip" to={stockHref(s.instrument_id, releaseId)} title={s.code ?? undefined}>
      <span>{s.name ?? s.code ?? s.instrument_id.slice(0, 8)}</span>
      {s.is_limit_up && s.limit_streak > 1 ? <i className="mono">{s.limit_streak}板</i> : null}
      <b className={`mono ${tone}`}>{pctText(s.pct_change) ?? '—'}</b>
    </Link>
  );
}

function Industries({ d }: { d: CrowdSnapshot }) {
  if (d.industries.length === 0) return <EmptyState>当天没有行业出现 2 只以上涨停（或发布里没有板块总览）。</EmptyState>;
  return (
    <ol className="fm-list" aria-label="行业扩散">
      {d.industries.map((it) => (
        <li key={it.node_id}>
          <header>
            <strong>{it.name}</strong>
            <span className="mono fm-up">{it.limit_ups} 涨停</span>
            <small className="muted mono">{it.observed}/{it.members} 只有行情 · 均 {pctText(it.avg_pct) ?? '缺失'}</small>
          </header>
          {it.leader && <div className="fm-row"><span className="fm-role">龙头</span><StockChip s={it.leader} releaseId={d.release_id} /></div>}
          {it.followers.length > 0 && (
            <div className="fm-row"><span className="fm-role">跟风</span>{it.followers.map((f) => <StockChip key={f.instrument_id} s={f} releaseId={d.release_id} />)}</div>
          )}
          <div className="fm-spread" role="list" aria-label={`${it.name} 近 ${it.spread.length} 个交易日扩散`}>
            {it.spread.map((day) => (
              <span key={day.session} role="listitem" className="small mono" title={`${day.session}：${day.strong_followers} 只涨幅 ≥3% 且未涨停`}>
                <span className="muted">{day.session.slice(5)}</span> {spreadCell(day)}
              </span>
            ))}
          </div>
        </li>
      ))}
    </ol>
  );
}

function InstitutionRows({ rows, releaseId }: { rows: InstitutionRow[]; releaseId: string }) {
  return (
    <table className="fm-table">
      <thead><tr><th scope="col">个股</th><th scope="col">机构净买入</th><th scope="col">买入</th><th scope="col">卖出</th></tr></thead>
      <tbody>
        {rows.map((r) => {
          const net = netText(r.net_cny);
          return (
            <tr key={r.instrument_id}>
              <th scope="row"><Link to={stockHref(r.instrument_id, releaseId)}>{r.name ?? r.code ?? r.instrument_id.slice(0, 8)}</Link></th>
              <td className={`mono ${r.net_cny != null && Number(r.net_cny) > 0 ? 'fm-up' : r.net_cny != null && Number(r.net_cny) < 0 ? 'fm-down' : ''}`}>
                {net ?? <MissingText>缺失</MissingText>}{net == null && r.net_reason ? <span className="muted small"> {r.net_reason}</span> : null}
              </td>
              <td className="mono">{netText(r.buy_cny)?.replace('+', '') ?? '—'}</td>
              <td className="mono">{netText(r.sell_cny)?.replace('+', '') ?? '—'}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Institutions({ d }: { d: CrowdSnapshot }) {
  const i = d.institutions;
  if (i.status === 'no_seat_reports') return <EmptyState>{i.notes[0] ?? '没有这个交易日的龙虎榜报告。'}</EmptyState>;
  if (i.status === 'no_institution_rows') return <EmptyState>{i.notes[0] ?? '报告里没有机构专用席位。'}</EmptyState>;
  return (
    <>
      <InstitutionRows rows={i.rows} releaseId={d.release_id} />
      <p className="muted small">{i.basis}</p>
      {i.notes.map((n) => <p key={n} className="muted small">{n}</p>)}
    </>
  );
}

export function CrowdView({ d }: { d: CrowdSnapshot }) {
  return (
    <>
      <p className="muted small">{d.method}</p>
      <div className="fm-grid">
        <section aria-label="行业扩散"><h4>行业扩散</h4><Industries d={d} /></section>
        <section aria-label="机构净买入"><h4>龙虎榜机构净买入{d.institutions.session_date ? <span className="muted small"> {d.institutions.session_date}</span> : null}</h4><Institutions d={d} /></section>
      </div>
      <section aria-label="同字联动" className="fm-names">
        <h4>同字联动 <Badge tone="warn">暂无数据 · 建议退役</Badge></h4>
        <p className="small">{d.name_clusters.reason}</p>
      </section>
      <ul className="small muted">{d.notes.map((n) => <li key={n}>{n}</li>)}</ul>
    </>
  );
}

export function CrowdPanel({ releaseId }: { releaseId: string | null }) {
  const q = useQuery({
    queryKey: ['bf-fillm', 'crowd', releaseId],
    queryFn: ({ signal }) => fillm.crowd({ release_id: releaseId as string }, signal),
    enabled: !!releaseId,
    staleTime: 60_000,
  });
  const d = q.data?.data;
  return (
    <section className="card" aria-label="抱团与联动">
      <SectionTitle title="抱团与联动" hint="行业扩散：当天有 2 只以上涨停的行业，龙头、跟风和近几日扩散；机构净买入来自龙虎榜。" />
      {q.isLoading && releaseId && <Loading label="读取抱团与联动…" />}
      {q.error && !d && <ErrorState error={q.error} what="抱团与联动" onRetry={() => void q.refetch()} />}
      {d && <CrowdView d={d} />}
    </section>
  );
}
