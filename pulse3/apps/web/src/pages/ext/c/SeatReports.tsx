/** Dragon-tiger-list reports of one trade date. Rows are shown as published: no netting, same seat on both sides = two rows. */
import { Badge } from '../../../components/Badges';
import { RESOLUTION_LABEL } from '../../../lib/format';
import type { Tone } from '../../../lib/version';
import { StockLink } from './common';
import { fmtCnyAmount } from './fmt';
import type { SeatPick } from './SeatProfile';
import type { SeatReport } from './types';

const RES_TONE: Record<string, Tone> = { resolved: 'good', ambiguous: 'warn', unresolved: 'bad', report_local_label: 'neutral' };

export function ResolutionBadge({ status }: { status: string }) {
  return <Badge tone={RES_TONE[status] ?? 'neutral'} title={`resolution_status = ${status}`}>{RESOLUTION_LABEL[status] ?? status}</Badge>;
}

type Row = SeatReport['rows'][number];

function Side({ title, rows, onPick }: { title: string; rows: Row[]; onPick: (p: SeatPick) => void }) {
  return (
    <div>
      <h4>{title}</h4>
      {rows.length === 0 ? <p className="muted small">没有披露行。</p> : (
        <table className="table">
          <caption className="sr-only">{title}</caption>
          <thead><tr><th scope="col">名次</th><th scope="col">席位（披露原文）</th><th scope="col" className="num">金额</th><th scope="col">识别</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.side}-${r.rank}-${r.raw_seat_name}-${i}`}>
                <td className="mono">{r.rank}</td>
                <td>{r.raw_seat_name}</td>
                <td className="num" title={`${r.canonical_amount_cny} 元`}>{fmtCnyAmount(r.canonical_amount_cny)}</td>
                <td>
                  <ResolutionBadge status={r.resolution_status} />
                  {r.resolution_status === 'resolved' && r.seat_id && r.seat_version != null && (
                    <button className="link-btn" aria-label={`查看席位画像：${r.raw_seat_name}`} onClick={() => onPick({ seat_id: r.seat_id as string, seat_version: r.seat_version as number, name: r.raw_seat_name })}>画像</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function SeatReportCard({ report, onPick }: { report: SeatReport; onPick: (p: SeatPick) => void }) {
  const buy = report.rows.filter((r) => r.side === 'buy');
  const sell = report.rows.filter((r) => r.side === 'sell');
  return (
    <article className="card item">
      <div className="item-head">
        <StockLink id={report.instrument_id} />
        <Badge>{report.disclosure_reason}</Badge>
        <span className="muted small">统计区间 {report.window_start} ~ {report.window_end}</span>
        <span className="grow" />
        <span className="muted small">修订 r{report.revision} · 来源 {report.source_id}</span>
      </div>
      <div className="seat-side">
        <Side title="买入席位" rows={buy} onPick={onPick} />
        <Side title="卖出席位" rows={sell} onPick={onPick} />
      </div>
      <p className="muted small">金额按披露原样显示，页面不计算净额；同一席位同时出现在买卖两侧时保留为两行。</p>
    </article>
  );
}
