/** 告警: user alerts in a window grouped by tier (P0/P1/P2 from the evidence artifact), delivery state badges, rule editor. */
import { useQueries } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Badge } from '../../../components/Badges';
import { EmptyState, Loading, SectionTitle } from '../../../components/States';
import { fmtTime } from '../../../lib/format';
import type { Tone } from '../../../lib/version';
import { AlertRuleEditor } from './AlertRuleEditor';
import { CHANNEL_LABEL, DELIVERY_LABEL, TIER_LABEL, TIER_ORDER, TIER_SHORT, groupAlertsByTier, parseDelivery, parseEvidence, type AlertItem, type TierKey } from './alertTier';
import { fetchArtifact } from './api';
import { ArtifactView, ExtError, LoadMore, Strip, StockLink, useNowAnchor, usePaged } from './common';
import { windowLastDays } from './time';
import type { AlertView, ListUserAlertsData } from './types';

const DELIVERY_TONE: Record<string, Tone> = { confirmed: 'good', accepted: 'good', pending: 'neutral', sending: 'neutral', failed: 'bad', unknown: 'warn', expired: 'warn' };
const DAYS = [1, 7, 30] as const;

export function DeliveryBadges({ states }: { states: string[] }) {
  if (states.length === 0) return <Badge tone="neutral">暂无投递记录</Badge>;
  return (
    <span className="chips">
      {states.map((s, i) => {
        const d = parseDelivery(s);
        return <Badge key={`${s}${i}`} tone={DELIVERY_TONE[d.state] ?? 'neutral'} title={`原始状态：${s}`}>{CHANNEL_LABEL[d.channel]} · {DELIVERY_LABEL[d.state] ?? d.state}</Badge>;
      })}
    </span>
  );
}

function fmtHeld(v: unknown): string {
  if (v === null || v === undefined) return '未标明';
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (Array.isArray(v)) return v.length > 0 ? v.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join('、') : '无';
  return typeof v === 'string' ? v : JSON.stringify(v);
}

function AlertRow({ item }: { item: AlertItem }) {
  const { alert: a, evidence: ev } = item;
  const [open, setOpen] = useState(false);
  return (
    <li className={`card item tier-${ev?.tier ?? 'unknown'}`}>
      <div className="item-head">
        <b>{ev ? ev.headline : '（证据读取中或不可用）'}</b>
        {ev?.direction && <Badge tone={ev.direction === 'negative' ? 'bad' : 'good'}>{ev.direction === 'negative' ? '负面' : '正面'}</Badge>}
        {ev?.category && <Badge>{ev.category}</Badge>}
        <span className="grow" />
        <span className="muted small">{fmtTime(a.raised_at)}</span>
      </div>
      <div className="item-head"><DeliveryBadges states={a.delivery_states} />{a.outcome_artifact && <Badge tone="neutral">有处理结果</Badge>}</div>
      {ev?.verification_note && <p className="flag flag-warn" style={{ margin: 0 }}>{ev.verification_note}</p>}
      {ev && ev.instruments.length > 0 && <div className="inst-line">{ev.instruments.map((i) => <StockLink key={i.instrument_id} id={i.instrument_id} code={i.code} name={i.name} />)}</div>}
      <div><button className="link-btn" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起依据' : '查看依据'}</button></div>
      {open && (
        <div className="sub-panel">
          {ev ? (
            <dl className="kv compact">
              <dt>触发原因</dt><dd>{ev.reason ?? <span className="missing">缺失</span>}</dd>
              <dt>来源核验</dt><dd>{ev.claim_status ?? <span className="missing">缺失</span>}{ev.verification_note ? `：${ev.verification_note}` : ''}</dd>
              <dt>持仓所在行业</dt><dd>{fmtHeld(ev.held_in_industry)}</dd>
              <dt>来源声明</dt><dd className="small mono">{ev.source_claim_revision_ids.length > 0 ? ev.source_claim_revision_ids.join('、') : '无'}</dd>
              <dt>行业</dt><dd>{ev.industries.length > 0 ? ev.industries.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join('、') : '无'}</dd>
              <dt>市场是否已反应</dt><dd>{ev.already_reacted ? <code className="small">{JSON.stringify(ev.already_reacted)}</code> : '未标明'}</dd>
              <dt>规则</dt><dd className="small mono">{a.rule_id.slice(0, 8)} v{a.rule_version}</dd>
            </dl>
          ) : <EmptyState>证据内容不可用。</EmptyState>}
          {a.outcome_artifact && <><h4>处理结果</h4><ArtifactView artifact={a.outcome_artifact} /></>}
        </div>
      )}
    </li>
  );
}

export function AlertsPage() {
  const [now, refresh] = useNowAnchor();
  const [days, setDays] = useState<number>(7);
  const [tier, setTier] = useState<TierKey | 'all'>('all');
  const win = useMemo(() => windowLastDays(days, now), [days, now]);
  const paged = usePaged<ListUserAlertsData>('list-user-alerts', { window: win });
  const alerts: AlertView[] = paged.pages.flatMap((p) => p.data.alerts);
  const evQ = useQueries({
    queries: alerts.map((a) => ({
      queryKey: ['extc', 'artifact', a.evidence_artifact.ref], queryFn: ({ signal }: { signal: AbortSignal }) => fetchArtifact(a.evidence_artifact, signal), staleTime: Infinity,
    })),
  });
  const items: AlertItem[] = alerts.map((a, i) => ({ alert: a, evidence: parseEvidence(evQ[i]?.data?.data.body) }));
  const groups = groupAlertsByTier(items);
  const counts = Object.fromEntries(groups.map((g) => [g.tier, g.items.length])) as Partial<Record<TierKey, number>>;
  const shown = groups.filter((g) => tier === 'all' || g.tier === tier);

  return (
    <div className="page">
      <SectionTitle title="告警" hint="P0 持仓负面 / P1 持仓所在行业的政策负面 / P2 自选或正面 / 规则（你自己设置的个股或行业规则）。分级和标题来自每条告警的证据，证据未读到时归入「未分级」。" right={<button className="btn btn-small" onClick={refresh}>刷新</button>} />
      <div className="filters">
        <label className="select-label">时间范围
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>{DAYS.map((d) => <option key={d} value={d}>最近 {d} 天</option>)}</select>
        </label>
        <div className="segmented" role="group" aria-label="按分级筛选">
          <button className={tier === 'all' ? 'on' : ''} aria-pressed={tier === 'all'} onClick={() => setTier('all')}>全部 {alerts.length}</button>
          {TIER_ORDER.map((t) => <button key={t} className={tier === t ? 'on' : ''} aria-pressed={tier === t} onClick={() => setTier(t)}>{TIER_SHORT[t]} {counts[t] ?? 0}</button>)}
        </div>
      </div>
      <Strip meta={paged.pages[0]?.meta} title="告警列表" />
      {paged.isLoading ? <Loading /> : paged.error ? <ExtError error={paged.error} onRetry={paged.refetch} what="告警" /> : alerts.length === 0 ? <EmptyState>这段时间没有告警。</EmptyState> : shown.length === 0 ? <EmptyState>所选分级下没有告警。</EmptyState> : (
        shown.map((g) => (
          <section key={g.tier} className="tier-group" aria-label={TIER_LABEL[g.tier]}>
            <h3>{TIER_LABEL[g.tier]}（{g.items.length}）</h3>
            <ul className="cards-list">{g.items.map((it) => <AlertRow key={it.alert.alert_id} item={it} />)}</ul>
          </section>
        ))
      )}
      <LoadMore paged={paged} />
      <AlertRuleEditor />
    </div>
  );
}
