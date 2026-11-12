/** 龙虎榜席位: reports by trade date, seat directory search, profile of a resolved seat. */
import { useMemo, useState } from 'react';
import { EmptyState, SectionTitle } from '../../../components/States';
import { ZERO_UUID } from './api';
import { LoadMore, Pending, Strip, useNowAnchor, usePaged } from './common';
import { SeatProfile, type SeatPick } from './SeatProfile';
import { ResolutionBadge, SeatReportCard } from './SeatReports';
import { isoSeconds, lastWeekday } from './time';
import type { SeatDirectoryData, SeatReportsData } from './types';

function Directory({ term, onPick }: { term: string; onPick: (p: SeatPick) => void }) {
  const paged = usePaged<SeatDirectoryData>('query-seat-directory', { snapshot_id: ZERO_UUID, name_query: term }, 30, term !== '');
  const seats = paged.pages.flatMap((p) => p.data.seats);
  return (
    <section aria-label="席位搜索结果">
      <Pending loading={paged.isLoading} error={paged.error} onRetry={paged.refetch} what="席位目录" />
      {!paged.isLoading && !paged.error && seats.length === 0 && <EmptyState>没有找到名称包含「{term}」的席位。</EmptyState>}
      <ul className="rows">
        {seats.map((s, i) => (
          <li key={`${s.seat_id ?? 'local'}-${s.display_name}-${i}`} className="row">
            <div className="row-main">
              <b>{s.display_name}</b>
              {s.report_local_ref && <div className="muted small">仅为报告内标签：{s.report_local_ref.side === 'buy' ? '买入' : '卖出'}第 {s.report_local_ref.rank} 名，没有对应的稳定席位</div>}
            </div>
            <ResolutionBadge status={s.resolution_status} />
            {s.resolution_status === 'resolved' && s.seat_id && s.seat_version != null
              ? <button className="btn btn-small" onClick={() => onPick({ seat_id: s.seat_id as string, seat_version: s.seat_version as number, name: s.display_name })}>查看画像</button>
              : <span className="muted small">无画像</span>}
          </li>
        ))}
      </ul>
      <LoadMore paged={paged} />
    </section>
  );
}

export function SeatsPage() {
  const [now] = useNowAnchor();
  const [date, setDate] = useState(() => lastWeekday(now));
  const [query, setQuery] = useState('');
  const [term, setTerm] = useState('');
  const [seat, setSeat] = useState<SeatPick | null>(null);
  const asOf = useMemo(() => isoSeconds(now), [now]);
  const dateOk = /^\d{4}-\d{2}-\d{2}$/.test(date);
  const reports = usePaged<SeatReportsData>('query-seat-reports', { snapshot_id: ZERO_UUID, trade_date: date, decision_as_of: asOf }, 20, dateOk);
  const list = reports.pages.flatMap((p) => p.data.reports);

  return (
    <div className="page">
      <SectionTitle title="龙虎榜席位" hint="按交易日查看披露的买卖席位。席位金额照披露原样显示；只有「已确认席位」才有画像，歧义或未识别的席位不会被强行归并。" />
      <div className="filters">
        <label className="select-label">交易日<input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>
        <form className="filters" role="search" onSubmit={(e) => { e.preventDefault(); setTerm(query.trim()); }}>
          <label className="select-label">席位名称<input className="wide" value={query} placeholder="例如：机构专用、某某证券营业部" onChange={(e) => setQuery(e.target.value)} /></label>
          <button className="btn btn-small">搜索席位</button>
          {term && <button type="button" className="btn btn-small btn-ghost" onClick={() => { setTerm(''); setQuery(''); }}>清除</button>}
        </form>
      </div>
      {seat && <SeatProfile seat={seat} nowMs={now} onClose={() => setSeat(null)} />}
      {term && <Directory term={term} onPick={setSeat} />}
      <h3>{date} 披露报告</h3>
      <Strip meta={reports.pages[0]?.meta} title="龙虎榜报告" />
      <Pending loading={dateOk && reports.isLoading} error={reports.error} onRetry={reports.refetch} what="龙虎榜报告" />
      {!dateOk && <p className="field-error" role="alert">请选择有效的交易日。</p>}
      {dateOk && !reports.isLoading && !reports.error && list.length === 0 && <EmptyState>这个交易日没有龙虎榜报告（可能是非交易日，或数据尚未发布）。</EmptyState>}
      <div className="cards-list">{list.map((r) => <SeatReportCard key={r.report_revision_id} report={r} onPick={setSeat} />)}</div>
      <LoadMore paged={reports} />
    </div>
  );
}
