/**
 * Shared list rows: instruments in a ranking / pool / watchlist, and the
 * fast-heating component panel. Order is always the server order.
 */
import { Link } from 'react-router-dom';
import type { AnalysisRelease, CandidatePoolItem, RankingEntry } from '../api/types';
import { useInstrumentLabel } from '../hooks/useInstrumentLabel';
import { useWatchlists } from '../hooks/useUserState';
import { ACTION_STATUS_LABEL, POOL_STATE_LABEL, fmtTime } from '../lib/format';
import { toFixed, toPercent } from '../lib/decimal';
import { Badge, MissingText } from './Badges';
import { DetailsButton } from './Diagnostics';
import type { Tone } from '../lib/version';

export function stockHref(instrumentId: string, releaseId: string | null, horizonKey?: string): string {
  const p = new URLSearchParams();
  if (releaseId) p.set('release', releaseId);
  if (horizonKey) p.set('h', horizonKey);
  const s = p.toString();
  return `/stock/${instrumentId}${s ? `?${s}` : ''}`;
}

export function InstrumentName({ instrumentId, releaseId }: { instrumentId: string; releaseId: string | null }) {
  const l = useInstrumentLabel(instrumentId, releaseId);
  return (
    <span className="inst-name">
      <b>{l.name}</b>
      {l.symbol && <span className="muted small mono"> {l.symbol}</span>}
      {l.isRisk && <Badge tone="warn" title="风险警示股票">风险警示</Badge>}
      {l.tradingStatus && l.tradingStatus !== 'active' && <Badge tone="bad">{l.tradingStatus}</Badge>}
    </span>
  );
}

export function WatchStar({ instrumentId, releaseId }: { instrumentId: string; releaseId: string | null }) {
  const wl = useWatchlists();
  const on = wl.has(instrumentId);
  return (
    <button
      className={`star ${on ? 'on' : ''}`}
      aria-pressed={on}
      aria-label={on ? '从自选移除' : '加入自选'}
      title={on ? '从自选移除' : '加入自选'}
      disabled={wl.saving || wl.isLoading}
      onClick={() => wl.toggle(instrumentId, releaseId)}
    >{on ? '★' : '☆'}</button>
  );
}

const STATUS_TONE: Record<RankingEntry['action_status'], Tone> = { actionable: 'good', watching: 'neutral', blocked: 'warn', missing_data: 'bad' };

export function RankingRows({ entries, releaseId, horizonKey }: { entries: readonly RankingEntry[]; releaseId: string | null; horizonKey?: string }) {
  return (
    <ol className="rows" aria-label="排名列表（顺序由服务端固定）">
      {entries.map((e) => {
        const prob = toPercent(e.calibrated_probability, 1);
        return (
          <li key={e.instrument_id} className="row">
            <span className="rank" title="研究机会顺序 alpha_rank">{e.alpha_rank}</span>
            <div className="row-main">
              <Link to={stockHref(e.instrument_id, releaseId, horizonKey)} className="row-link">
                <InstrumentName instrumentId={e.instrument_id} releaseId={releaseId} />
              </Link>
              <div className="row-sub">
                {prob ? <Badge tone="good" title="仅在事件定义并校准后才显示概率">校准概率 {prob}</Badge> : <span className="muted small">相对分 {toFixed(e.relative_rank_score, 2) ?? <MissingText />}，不是上涨概率</span>}
                {e.plan_ref === null && <Badge tone="warn" title="本次发布没有为它生成计划">无交易计划</Badge>}
              </div>
            </div>
            <div className="row-side">
              <Badge tone={STATUS_TONE[e.action_status]}>{ACTION_STATUS_LABEL[e.action_status]}</Badge>
              {e.action_rank !== null && <span className="muted small">行动序 {e.action_rank}</span>}
            </div>
            <WatchStar instrumentId={e.instrument_id} releaseId={releaseId} />
            <DetailsButton title="排名解释" rows={[
              ['instrument_id', e.instrument_id], ['alpha_rank', String(e.alpha_rank)], ['action_rank', String(e.action_rank ?? '无')],
              ['relative_rank_score', e.relative_rank_score], ['explanation_ref', e.explanation_ref],
              ['plan_ref', e.plan_ref ? `${e.plan_ref.plan_id} v${e.plan_ref.plan_version}` : '无'],
            ]} />
          </li>
        );
      })}
    </ol>
  );
}

const POOL_TONE: Record<CandidatePoolItem['current_state'], Tone> = { observing: 'neutral', qualified: 'good', actionable: 'good', cooling: 'warn', removed: 'bad' };

export function PoolRows({ items, releaseId }: { items: readonly CandidatePoolItem[]; releaseId: string | null }) {
  return (
    <ol className="rows" aria-label="升温候选（按入池事件顺序）">
      {items.map((it) => (
        <li key={it.pool_event_id} className="row">
          <span className="rank">{it.event_sequence}</span>
          <div className="row-main">
            <Link to={stockHref(it.instrument_id, releaseId)} className="row-link"><InstrumentName instrumentId={it.instrument_id} releaseId={releaseId} /></Link>
            <div className="row-sub muted small">所属板块 {it.taxonomy_node.node_id} · 触发 {it.trigger_code} · {fmtTime(it.available_at)}</div>
          </div>
          <div className="row-side">
            <Badge tone={POOL_TONE[it.current_state]}>{POOL_STATE_LABEL[it.current_state]}</Badge>
            {it.plan_ref === null && <span className="muted small">尚无计划</span>}
          </div>
          <WatchStar instrumentId={it.instrument_id} releaseId={releaseId} />
        </li>
      ))}
    </ol>
  );
}

/**
 * Fast-heating components (spec 6). Each surge component is shown with the state the
 * release reports for it; anything not READY, or absent, reads 缺失 - never 0.
 */
export const SURGE_COMPONENTS: Array<{ key: string; label: string; what: string }> = [
  { key: 'relative_strength_change', label: '相对强度变化', what: '板块相对大盘的强弱在加速' },
  { key: 'volume_turnover_acceleration', label: '成交与换手加速', what: '成交额、换手率异常放大' },
  { key: 'breadth_diffusion', label: '上涨家数扩散', what: '板块内越来越多股票上涨' },
  { key: 'leader_confirmation', label: '龙头确认', what: '板块龙头走势确认' },
  { key: 'news_novelty', label: '消息新颖性', what: '出现新的、未被反映的消息' },
  { key: 'community_growth', label: '社区讨论增速', what: '讨论热度相对自身基线上升' },
  { key: 'fund_proxy', label: '资金代理指标', what: '资金流向的代理信号（口径因平台而异）' },
];

export function SurgeComponents({ release }: { release: AnalysisRelease | undefined }) {
  if (!release) return <p className="muted small">读取发布版本中…</p>;
  return (
    <div className="surge" role="list" aria-label="升温组成项">
      {SURGE_COMPONENTS.map((c) => {
        const comp = release.components.find((x) => x.component_key === `heating.${c.key}`);
        const ready = comp?.component_state === 'READY';
        const stateText = !comp ? '缺失' : comp.component_state === 'READY' ? '已就绪' : comp.component_state === 'MISSING' ? '缺失' : comp.component_state === 'STALE' ? '已过期' : '计算失败';
        return (
          <div key={c.key} role="listitem" className={`surge-item ${ready ? '' : 'surge-missing'}`}>
            <div><b>{c.label}</b></div>
            <div className="muted small">{c.what}</div>
            <div>{ready ? <Badge tone="good">{stateText}（{fmtTime(comp?.computed_at)}）</Badge> : comp && comp.component_state !== 'MISSING' ? <Badge tone={comp.component_state === 'STALE' ? 'warn' : 'bad'}>{stateText}，不计入</Badge> : <Badge tone="bad"><MissingText>{stateText}</MissingText></Badge>}</div>
          </div>
        );
      })}
    </div>
  );
}
