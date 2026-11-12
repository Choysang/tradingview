/** 财经日历: calendar events from the latest calendar snapshot, grouped by month; a row expands to industry -> stocks links. */
import { useQueries, useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Badge } from '../../../components/Badges';
import { EmptyState, Loading, SectionTitle } from '../../../components/States';
import { fmtTime } from '../../../lib/format';
import type { Tone } from '../../../lib/version';
import { ZERO_UUID, explainError, extGet } from './api';
import { ExtError, LoadMore, Pending, StockLink, Strip, useNowAnchor, usePaged } from './common';
import { CALENDAR_STATUS_LABEL, eventTimeLabel, fmtMeasurement, groupByMonth } from './fmt';
import { isoSeconds, shanghaiDayStart, shanghaiDate, windowNextDays } from './time';
import type { CalendarEventsData, CalendarLinksData, CalendarName, CalendarNamesData, CalendarRevision } from './types';

const STATUS_TONE: Record<string, Tone> = { scheduled: 'neutral', postponed: 'warn', cancelled: 'bad', occurred: 'good', corrected: 'warn' };
const DAY = 86_400_000;

function Links({ ev, decisionAsOf }: { ev: CalendarRevision; decisionAsOf: string }) {
  const q = useQuery({
    queryKey: ['extc', 'calendar-links', ev.calendar_event_id, decisionAsOf],
    queryFn: ({ signal }) => extGet<CalendarLinksData>('c/calendar-links', { calendar_event_id: ev.calendar_event_id, decision_as_of: decisionAsOf }, signal),
  });
  if (q.isLoading) return <Loading label="读取关联行业…" />;
  if (q.error) return <ExtError error={q.error} onRetry={() => { void q.refetch(); }} what="关联行业" />;
  const links = q.data?.data.links ?? [];
  if (links.length === 0) return <EmptyState>这个事件暂时没有关联行业。</EmptyState>;
  return (
    <ul className="plain">
      {links.map((l) => (
        <li key={l.industry_node_id}>
          <b>{l.industry_name}</b> <span className="muted small">关联规则 {String(l.link_rule_version)}</span>
          <div className="inst-line">{l.instruments.length === 0 ? <span className="muted small">该行业下暂无个股</span> : l.instruments.map((i) => <StockLink key={i.instrument_id} id={i.instrument_id} code={i.code} name={i.name} />)}</div>
        </li>
      ))}
    </ul>
  );
}

function EventRow({ ev, info, decisionAsOf }: { ev: CalendarRevision; info: CalendarName | undefined; decisionAsOf: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr>
        <td>{info ? <b>{info.name}</b> : <span className="muted">事件 {ev.calendar_event_id.slice(0, 8)}</span>}{info && (info.category || info.region) && <div className="muted small">{[info.category, info.region].filter(Boolean).join(' · ')}</div>}</td>
        <td>{eventTimeLabel(ev)}</td>
        <td><Badge tone={STATUS_TONE[ev.status] ?? 'neutral'} title={`status = ${ev.status}`}>{CALENDAR_STATUS_LABEL[ev.status] ?? ev.status}</Badge></td>
        <td>{fmtMeasurement(ev.expected)}</td>
        <td>{fmtMeasurement(ev.actual)}</td>
        <td className="mono small">r{ev.revision}</td>
        <td className="small muted">{fmtTime(ev.available_at)}</td>
        <td><button className="link-btn" aria-expanded={open} aria-label={`${open ? '收起' : '展开'}事件 ${ev.calendar_event_id.slice(0, 8)} 的关联行业`} onClick={() => setOpen(!open)}>{open ? '收起' : '关联行业'}</button></td>
      </tr>
      {open && <tr><td colSpan={8}><div className="sub-panel"><Links ev={ev} decisionAsOf={decisionAsOf} /></div></td></tr>}
    </>
  );
}

export function CalendarPage() {
  const [now, refresh] = useNowAnchor();
  const [from, setFrom] = useState(() => shanghaiDate(now));
  const [to, setTo] = useState(() => shanghaiDate(now + 30 * DAY));
  const decisionAsOf = useMemo(() => isoSeconds(now), [now]);
  const start = shanghaiDayStart(from);
  const endDay = shanghaiDayStart(to);
  const valid = start !== null && endDay !== null && start < endDay;
  // the end date is inclusive for the user, so the window ends at the start of the next day
  const win = valid ? { start, end: isoSeconds(Date.parse(endDay) + DAY) } : windowNextDays(30, now);
  const paged = usePaged<CalendarEventsData>('query-calendar-events', { snapshot_id: ZERO_UUID, decision_as_of: decisionAsOf, window: win }, 50, valid);
  const events = paged.pages.flatMap((p) => p.data.events);
  const groups = groupByMonth(events);
  // Names are not part of CalendarRevision: one request per page of events (<= 200 ids each).
  const nameQs = useQueries({
    queries: paged.pages.map((p) => {
      const ids = p.data.events.map((e) => e.calendar_event_id).join(',');
      return { queryKey: ['extc', 'calendar-names', ids], queryFn: ({ signal }: { signal: AbortSignal }) => extGet<CalendarNamesData>('c/calendar-names', { ids }, signal), enabled: ids !== '', staleTime: 60_000 };
    }),
  });
  const names: Record<string, CalendarName> = Object.assign({}, ...nameQs.map((q) => q.data?.data.names ?? {}));
  const namesFailed = nameQs.find((q) => q.error)?.error;

  return (
    <div className="page">
      <SectionTitle title="财经日历" hint="事件按当前最新日历展示，每条是事件的最新修订。只有日期没有时间的事件不会显示具体时刻；预期值与实际值缺失时显示「—」或「缺失」，不当作 0。" right={<button className="btn btn-small" onClick={refresh}>刷新</button>} />
      <div className="filters">
        <label className="select-label">开始日期<input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
        <label className="select-label">结束日期<input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label>
      </div>
      {!valid && <p className="field-error" role="alert">请选择有效的日期范围（结束日期不早于开始日期）。</p>}
      <Strip meta={paged.pages[0]?.meta} title="财经日历" />
      {namesFailed != null && <div className="notice notice-warn" role="alert">事件名称读取失败（{explainError(namesFailed)}），暂以编号代替；时间与状态不受影响。</div>}
      <Pending loading={paged.isLoading} error={paged.error} onRetry={paged.refetch} what="财经日历" />
      {valid && !paged.isLoading && !paged.error && events.length === 0 && <EmptyState>这段时间没有日历事件。</EmptyState>}
      {groups.map((g) => (
        <section key={g.month} aria-label={g.month}>
          <h3 className="month-head">{g.month}（{g.items.length}）</h3>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th scope="col">事件</th><th scope="col">时间</th><th scope="col">状态</th><th scope="col">预期</th><th scope="col">实际</th><th scope="col">修订</th><th scope="col">可用于决策的时间</th><th scope="col"><span className="sr-only">操作</span></th></tr></thead>
              <tbody>{g.items.map((ev) => <EventRow key={`${ev.calendar_event_id}-${ev.revision}`} ev={ev} info={names[ev.calendar_event_id]} decisionAsOf={decisionAsOf} />)}</tbody>
            </table>
          </div>
        </section>
      ))}
      <LoadMore paged={paged} />
    </div>
  );
}
