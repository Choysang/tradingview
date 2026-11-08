import { useEffect, useRef, useState } from 'react';
import { ArrowRight, ArrowUpRight, TrendingDown, TrendingUp } from 'lucide-react';
import { navigate } from '../../app/router';
import type { FilterInput } from '../../core/api';
import type { MacroDashboardSummary } from '../../core/contract';
import { clock, money, num, pct, signed, tone } from '../../core/format';
import { useMarket, useWorkspace } from '../../core/store';
import { LineChart, Spark } from '../../kit/charts';
import { Num, Panel, Skeleton } from '../../kit/primitives';
import { moodBand, moodTrend } from '../shell/mood';
import EventTape from './EventTape';
import Ladder from './Ladder';
import SealClock from './SealClock';
import './overview.css';

/** Jump to the screener with a prepared filter; the overview is a launchpad, not a dead end. */
export function screen(filters: FilterInput, ask = '') {
  useWorkspace.getState().replaceFilters(filters, ask);
  navigate('screener');
}

function useCountUp(target: number | null | undefined, duration = 900) {
  const [value, setValue] = useState<number | null>(null);
  const from = useRef<number | null>(null);
  useEffect(() => {
    if (target == null) return;
    const start = from.current ?? 0;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches || start === target) {
      setValue(target);
      from.current = target;
      return;
    }
    const begin = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const progress = Math.min(1, (now - begin) / duration);
      const eased = 1 - Math.pow(1 - progress, 4);
      setValue(start + (target - start) * eased);
      if (progress < 1) frame = requestAnimationFrame(step);
      else from.current = target;
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);
  return value;
}

const BAND_EDGES = [25, 40, 55, 70, 85];
const BAND_WORDS = ['冰点', '低迷', '中性', '升温', '亢奋', '高潮'];

function MoodHero({ overview }: { overview: MacroDashboardSummary }) {
  const score = overview.sentiment_score;
  const shown = useCountUp(score);
  const band = moodBand(score);
  const trend = moodTrend(overview.market_curve);
  const curve = overview.market_curve;
  return (
    <section className="hero" aria-labelledby="hero-title">
      <div className="hero-top">
        <span className="eyebrow">情绪脉搏 · {overview.provenance.trade_date} · {clock(overview.provenance.as_of)}</span>
        <span className={`trend ${trend.delta > 1.5 ? 'up' : trend.delta < -1.5 ? 'down' : ''}`}>
          {trend.delta >= 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
          {trend.label} <b className="num">{signed(trend.delta, 1)}</b>
        </span>
      </div>
      <div className="hero-main">
        <div className="hero-score">
          <span className="hero-number num" aria-hidden="true">{shown == null ? '—' : shown.toFixed(1)}</span>
          <h2 id="hero-title" className="sr-only">市场情绪 {num(score, 1)} 分，{band.word}</h2>
        </div>
        <div className="hero-word">
          <strong className="display">{band.word}</strong>
          <p>{band.hint}</p>
        </div>
      </div>
      <div className="gauge" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={score} aria-label="情绪温度">
        <div className="gauge-track">
          {BAND_EDGES.map(edge => <i key={edge} style={{ left: `${edge}%` }} />)}
          <span className="gauge-marker" style={{ left: `${Math.max(0, Math.min(100, score))}%` }} />
        </div>
        <div className="gauge-labels">
          {BAND_WORDS.map((word, index) => {
            const start = index === 0 ? 0 : BAND_EDGES[index - 1];
            const end = BAND_EDGES[index] ?? 100;
            return <span key={word} className={band.word === word ? 'active' : ''} style={{ left: `${(start + end) / 2}%` }}>{word}</span>;
          })}
        </div>
      </div>
      <div className="hero-curve">
        {curve.length > 1 ? (
          <LineChart
            height={116}
            labels={curve.map(point => point.time.slice(0, 5))}
            series={[{ key: 'mood', label: '情绪', values: curve.map(point => point.value), colour: '--mood', area: true, width: 1.8 }]}
            format={value => value.toFixed(1)}
          />
        ) : (
          <p className="faint hero-curve-empty">会话曲线随快照逐点生成。</p>
        )}
      </div>
    </section>
  );
}

function Vitals({ overview }: { overview: MacroDashboardSummary }) {
  const sealRate = overview.total_limit_up + overview.total_broken_board
    ? (overview.total_limit_up / (overview.total_limit_up + overview.total_broken_board)) * 100
    : null;
  const { up, down, flat } = overview.breadth;
  const total = up + down + flat || 1;
  return (
    <div className="vitals-grid">
      <button className="tile" onClick={() => screen({ pool_type: 'limit_up' })}>
        <span className="tile-label">涨停<ArrowUpRight size={13} /></span>
        <span className="tile-value up"><Num value={overview.total_limit_up} format={value => num(value, 0)} /><small>/ {overview.total_stocks}</small></span>
        <span className="tile-note">封板率 {pct(sealRate, 1, false)}</span>
      </button>
      <button className="tile" onClick={() => screen({ pool_type: 'broken_attack' })}>
        <span className="tile-label">炸板<ArrowUpRight size={13} /></span>
        <span className="tile-value down"><Num value={overview.total_broken_board} format={value => num(value, 0)} /></span>
        <span className="tile-note">炸板率 {pct(overview.broken_rate, 1, false)}</span>
      </button>
      <button className="tile gold-tile" onClick={() => screen({ pool_type: 'limit_up', limit_streak_exact: [overview.max_limit_height] })}>
        <span className="tile-label">空间高度<ArrowUpRight size={13} /></span>
        <span className="tile-value gold"><Num value={overview.max_limit_height} format={value => num(value, 0)} /><small>板</small></span>
        <span className="tile-note truncate">{overview.max_height_stock}</span>
      </button>
      <div className="tile">
        <span className="tile-label">昨涨停溢价</span>
        <span className={`tile-value ${tone(overview.yesterday_limit_up_premium)}`}><Num value={overview.yesterday_limit_up_premium} format={value => pct(value, 2)} /></span>
        <span className="tile-note">接力赚钱效应</span>
      </div>
      <div className="tile">
        <span className="tile-label">成交额</span>
        <span className="tile-value"><Num value={overview.total_amount} format={value => num(value, 0)} /><small>亿</small></span>
        <span className="tile-note">{overview.provenance.mode === 'SIM' ? '模拟覆盖' : '全市场'} {overview.total_stocks} 只</span>
      </div>
      <div className="tile">
        <span className="tile-label">涨跌比</span>
        <span className="tile-value"><span className="up num">{up}</span><small>:</small><span className="down num">{down}</span></span>
        <span className="split" aria-hidden="true">
          <i className="up-bar" style={{ width: `${(up / total) * 100}%` }} />
          <i className="flat-bar" style={{ width: `${(flat / total) * 100}%` }} />
          <i className="down-bar" style={{ width: `${(down / total) * 100}%` }} />
        </span>
      </div>
    </div>
  );
}

function Breadth({ overview }: { overview: MacroDashboardSummary }) {
  const { buckets, bucket_labels: labels } = overview.breadth;
  const max = Math.max(1, ...buckets);
  const half = buckets.length / 2;
  return (
    <Panel eyebrow="BREADTH" title="涨跌分布" subtitle="按涨幅区间计数，停牌不计">
      <div className="histogram">
        {buckets.map((count, index) => (
          <div key={labels[index]} className={`bin ${index < half ? 'down' : 'up'}`} style={{ '--h': `${(count / max) * 100}%`, '--i': index } as React.CSSProperties}>
            <span className="bin-count num">{count}</span>
            <span className="bin-bar" />
            <span className="bin-label num">{labels[index]}</span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function ThemePulse({ overview }: { overview: MacroDashboardSummary }) {
  const themes = overview.hot_themes.slice(0, 8);
  const maxLimit = Math.max(1, ...themes.map(theme => theme.limit_up_count));
  return (
    <Panel
      eyebrow="THEMES"
      title="题材共振"
      subtitle="强度 = 涨停 × 2 + 连阳 + 均涨幅 × 0.5"
      actions={<button className="btn sm ghost" onClick={() => navigate('themes')}>图谱<ArrowRight size={13} /></button>}
    >
      <ol className="theme-list">
        {themes.map((theme, index) => (
          <li key={theme.theme_name} style={{ '--i': index } as React.CSSProperties}>
            <button className="theme-row" onClick={() => screen({ selected_themes: [theme.theme_name] }, `#${theme.theme_name}`)}>
              <span className="theme-rank num">{String(index + 1).padStart(2, '0')}</span>
              <span className="theme-name">
                <strong className="truncate">{theme.theme_name}</strong>
                <small className="truncate">龙头 {theme.leader_name}</small>
              </span>
              <span className="theme-heat" aria-label={`涨停 ${theme.limit_up_count} 家`}>
                <i style={{ width: `${(theme.limit_up_count / maxLimit) * 100}%` }} />
                <b className="num">{theme.limit_up_count}</b>
              </span>
              <span className={`num theme-pct ${tone(theme.avg_pct_chg)}`}>{pct(theme.avg_pct_chg)}</span>
              <span className="theme-spark"><Spark values={theme.sparkline} width={72} height={22} baseline={0} label={`${theme.theme_name}均值走势`} /></span>
              <span className="num theme-amount muted">{money(theme.amount)}</span>
            </button>
          </li>
        ))}
      </ol>
    </Panel>
  );
}

function GrowthStrip({ overview }: { overview: MacroDashboardSummary }) {
  if (!overview.growth_streak_tiers.length) return null;
  const max = Math.max(1, ...overview.growth_streak_tiers.map(tier => tier.count));
  return (
    <Panel eyebrow="GROWTH" title="连阳梯度" subtitle="连续收涨天数；点击进入连阳池">
      <div className="growth-strip">
        {overview.growth_streak_tiers.map(tier => {
          const level = Number.parseInt(tier.tier_key, 10);
          return (
            <button key={tier.tier_key} className="growth-cell" onClick={() => screen({ pool_type: 'growth_streak', min_growth_streak: level }, `${level}连阳`)}>
              <span className="growth-level num">{level}<small>{level >= 7 ? '+' : ''} 连阳</small></span>
              <span className="growth-count num">{tier.count}</span>
              <span className="growth-bar"><i style={{ width: `${(tier.count / max) * 100}%` }} /></span>
              <span className="growth-top truncate">{tier.top_stock_name}</span>
            </button>
          );
        })}
      </div>
    </Panel>
  );
}

function OverviewSkeleton() {
  return (
    <div className="ov-grid" aria-busy="true">
      <div className="ov-hero"><Skeleton height={360} radius={20} /></div>
      <div className="ov-vitals"><Skeleton height={360} radius={14} /></div>
      <div className="ov-ladder"><Skeleton height={300} radius={14} /></div>
    </div>
  );
}

export default function OverviewView() {
  const overview = useMarket(state => state.overview);
  return (
    <div className="page-inner">
      {!overview ? <OverviewSkeleton /> : (
        <div className="ov-grid">
          <div className="ov-hero"><MoodHero overview={overview} /></div>
          <div className="ov-vitals"><Vitals overview={overview} /></div>
          <div className="ov-ladder"><Ladder overview={overview} /></div>
          <div className="ov-clock"><SealClock overview={overview} /></div>
          <div className="ov-breadth"><Breadth overview={overview} /></div>
          <div className="ov-themes"><ThemePulse overview={overview} /></div>
          <div className="ov-tape"><EventTape /></div>
          <div className="ov-growth"><GrowthStrip overview={overview} /></div>
        </div>
      )}
    </div>
  );
}
