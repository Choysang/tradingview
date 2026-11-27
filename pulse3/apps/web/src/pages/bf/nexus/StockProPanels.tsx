/**
 * Stock Workbench Pro Upgrade Panels (Nexus 2026 / 3.5):
 * 1. ActionSummary + RAPTOR Real-Time News Adjustment & Controlled LLM (gpt-6-luna) card.
 * 2. Multi-timeframe (日/周/月/年) TradingView 26-Indicator Technical Ratings Gauge (15 MAs + 11 Oscillators)
 *    + 6 Pivot Point systems + VPVR 24-bin Chip Profile + SuperTrend / BB-KC Squeeze / ADX(14) / Divergences.
 * 3. Interactive Intraday-T (ArchetypeTrader + Avellaneda-Stoikov) & Adaptive Grid Trading with
 *    ACM ICAIF 2026 Neurosymbolic Guards, crumble risk, budget notes & zero-lookahead backtest metrics.
 * 4. 4-Session Eastmoney Guba retail resonance & fact-checking + Famous LHB Seat Personas + Verified News.
 * 5. Full Zero-404 Fallback Stock Workbench with full TradingView-grade <PriceChart /> from pro.kline_bars.
 */
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import type { BarWindow, CanonicalBar } from '../../../api/types';
import { Badge, MissingText } from '../../../components/Badges';
import { WatchStar } from '../../../components/Lists';
import { MetaStrip } from '../../../components/MetaStrip';
import { type ChartLine, PriceChart } from '../../../components/PriceChart';
import { ReleaseBar } from '../../../components/ReleaseBar';
import { EmptyState, Loading, SectionTitle } from '../../../components/States';
import type { ReleaseCtx } from '../../../hooks/useRelease';
import { useTheme } from '../../../hooks/useTheme';
import { toChartNumber } from '../../../lib/decimal';
import { fmtMoney, fmtTime } from '../../../lib/format';
import {
  type CrumbleRiskLevel,
  fmtVal,
  type GridMode,
  type NexusStockProAnalysis,
  nexusOps,
  type PivotLevelSet,
  TV_LABEL_TEXT,
  TV_LABEL_TONE,
  VERDICT_TEXT,
} from './api';
import './nexus.css';

const TF_NAME: Record<string, string> = { '1d': '日线', '1w': '周线', '1mo': '月线', '1y': '年线' };
const PIVOT_NAME: Record<PivotLevelSet['method'], string> = {
  traditional: 'Traditional 传统',
  classic: 'Classic 经典',
  fibonacci: 'Fibonacci 斐波那契',
  camarilla: 'Camarilla 日内反转',
  woodie: 'Woodie 收盘加权',
  demark: 'DeMark 条件枢轴',
};

const SEAT_CATEGORY_BADGE: Record<string, { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  lhasa_retail_corps: { label: '拉萨天团 · 散户接盘预警', tone: 'bad' },
  one_day_dump_raider: { label: '一日游砸盘席位', tone: 'bad' },
  legacy_hijacked_seat: { label: '退役/李鬼借壳警示', tone: 'warn' },
  pattern_leader_synergy: { label: '格局主升顶级游资', tone: 'good' },
  institution_channel: { label: '机构 / 北向深沪股通', tone: 'good' },
  quant_high_freq: { label: '量化高频 T+0 席位', tone: 'warn' },
  regular_branch: { label: '普通营业部席位', tone: 'neutral' },
};

const CLAIM_VERDICT_BADGE: Record<string, { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  supported_by_facts: { label: '硬数据证实', tone: 'good' },
  contradicted_by_facts: { label: '已被数据证伪', tone: 'bad' },
  unverified_noise: { label: '无依据情绪噪音', tone: 'neutral' },
};

const DIRECTIVE_TEXT: Record<NexusStockProAnalysis['realtime_adjustment']['action_directive'], { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  catalyst_boost: { label: '催化正向微调', tone: 'good' },
  tighten_stop_grid: { label: '收紧止损与网格', tone: 'warn' },
  risk_veto: { label: '风控一票否决', tone: 'bad' },
  neutral_monitor: { label: '中性监控', tone: 'neutral' },
};

const CRUMBLE_BADGE: Record<CrumbleRiskLevel, { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  low: { label: '低溃散风险', tone: 'good' },
  moderate: { label: '中等流动性注意', tone: 'warn' },
  high: { label: '高溃散踩踏预警', tone: 'bad' },
  unknown: { label: '风险待评估', tone: 'neutral' },
};

const T_DIR_LABEL: Record<NexusStockProAnalysis['intraday_t_plan']['recommended_direction'], string> = {
  forward_t: '正T · 先买后卖',
  reverse_t: '倒T · 先卖后买',
  dual_vwap_t: '双边 VWAP 灵活做T',
  lock_hold: 'T+1 锁仓持有',
  exit_only: '仅高抛避险',
  unavailable: '暂不可做T',
};

const SQUEEZE_LABEL: Record<NexusStockProAnalysis['pro_signals']['volatility_squeeze'], string> = {
  squeeze_on: '⚡ 蓄势挤压中',
  squeeze_fired_up: '🚀 向上释放爆发',
  squeeze_fired_down: '⚠️ 向下破位释放',
  normal: '常态波动',
  unavailable: '预热缺失',
};

const CROWD_STATE_TEXT: Record<NexusStockProAnalysis['retail_resonance']['crowd_state'], { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  overheated_reversal_risk: { label: '情绪过热反转预警', tone: 'bad' },
  healthy_divergence: { label: '良性分歧承接', tone: 'good' },
  early_discovery: { label: '早期预期差发现', tone: 'good' },
  quiet_ignored: { label: '低关注冷清区', tone: 'neutral' },
  unavailable: { label: '暂无社区样本', tone: 'warn' },
};

const FLASH_VER_TEXT: Record<string, { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  official_confirmed: { label: '官方权威核验', tone: 'good' },
  multi_source_verified: { label: '多源交叉印证', tone: 'good' },
  single_source_flash: { label: '单源快讯待核', tone: 'warn' },
  contradicted: { label: '证伪/澄清', tone: 'bad' },
};

function needleLeftPct(scoreStr: string | null): string {
  if (scoreStr === null) return '50%';
  const n = Number(scoreStr);
  if (!Number.isFinite(n)) return '50%';
  const clamped = Math.max(-1, Math.min(1, n));
  return `${((clamped + 1) / 2) * 100}%`;
}

function fmtDivLabel(d: 'bullish_bottom_div' | 'bearish_top_div' | 'none'): { text: string; tone: 'good' | 'bad' } | null {
  if (d === 'bullish_bottom_div') return { text: '底背离', tone: 'good' };
  if (d === 'bearish_top_div') return { text: '顶背离', tone: 'bad' };
  return null;
}

export function buildBarWindowFromNexus(pro: NexusStockProAnalysis): BarWindow {
  const firstDate = pro.kline_bars[0]?.trade_date ?? pro.decision_as_of.slice(0, 10);
  const lastDate = pro.kline_bars[pro.kline_bars.length - 1]?.trade_date ?? pro.decision_as_of.slice(0, 10);
  const bars: CanonicalBar[] = pro.kline_bars.map((kb) => {
    const volNum = Number(kb.volume);
    return {
      instrument_id: pro.instrument_id,
      trade_date: kb.trade_date,
      timeframe: pro.timeframe,
      session_scope: 'regular',
      bar_open_time: `${kb.trade_date}T01:30:00Z`,
      bar_close_time: `${kb.trade_date}T07:00:00Z`,
      snapshot_id: pro.release_id,
      currency: 'CNY',
      adjustment_mode: 'raw',
      open_price_raw: kb.open,
      high_price_raw: kb.high,
      low_price_raw: kb.low,
      close_price_raw: kb.close,
      volume_shares: Number.isFinite(volNum) ? Math.round(volNum) : null,
      turnover_amount_cny: null,
      exchange_reference_price_raw: kb.open,
      is_final: true,
      available_at: pro.decision_as_of,
      quality_status: 'valid',
      field_quality: {},
    };
  });
  return {
    instrument_id: pro.instrument_id,
    snapshot_id: pro.release_id,
    timeframe: pro.timeframe,
    session_policy_version: 'cn-a-continuous-v1',
    adjustment_mode: pro.adjustment_mode === 'forward_adjusted' || pro.adjustment_mode === 'backward_adjusted' ? pro.adjustment_mode : 'raw',
    adjustment_version: null,
    start: `${firstDate}T00:00:00Z`,
    end: `${lastDate}T23:59:59Z`,
    window_complete: bars.length > 0,
    window_hash: pro.cache_key_hash,
    bars,
  };
}

export function NexusActionSummaryCard({ pro }: { pro: NexusStockProAnalysis }) {
  const ap = pro.action_plan;
  const rt = pro.realtime_adjustment;
  const vMeta = VERDICT_TEXT[ap.verdict] ?? { label: ap.verdict, tone: 'neutral' as const };
  const rtDir = DIRECTIVE_TEXT[rt.action_directive] ?? { label: rt.action_directive, tone: 'neutral' as const };

  return (
    <section className="card" aria-label="Nexus综合买卖点与持有计划">
      <SectionTitle
        title="Nexus 多维综合买卖点与持有计划"
        hint={pro.price_basis_note}
        right={(
          <div className="chips">
            <Badge tone={vMeta.tone}>{vMeta.label}</Badge>
            <Badge tone="neutral">综合评分 {fmtVal(ap.composite_score)}</Badge>
            {pro.cached_in_postgres && <Badge tone="good" title={`cache_key=${pro.cache_key_hash.slice(0, 12)}`}>PG工作台缓存命中</Badge>}
          </div>
        )}
      />

      <dl className="nx-price-strip">
        <div><dt>低吸买入区间</dt><dd>{fmtVal(ap.entry_low)} ~ {fmtVal(ap.entry_high)}</dd></div>
        <div><dt>硬性风控止损(1.8ATR)</dt><dd className="nx-down">{fmtVal(ap.stop_loss)}</dd></div>
        <div><dt>第一目标止盈(TP1)</dt><dd className="nx-up">{fmtVal(ap.target_1)}</dd></div>
        <div><dt>第二目标止盈(TP2)</dt><dd className="nx-up">{fmtVal(ap.target_2)}</dd></div>
        <div><dt>建议持有计划</dt><dd>{ap.holding_sessions}</dd></div>
        <div><dt>单票仓位上限</dt><dd>{ap.max_position_pct}</dd></div>
      </dl>

      <div className="nx-grid-4" style={{ marginTop: 8 }}>
        <div className="nx-gauge-box"><span className="muted small">技术面多周期分</span><b>{fmtVal(ap.pillar_scores.technical)}</b></div>
        <div className="nx-gauge-box"><span className="muted small">资金量能结构分</span><b>{fmtVal(ap.pillar_scores.capital_flow)}</b></div>
        <div className="nx-gauge-box"><span className="muted small">消息催化核验分</span><b>{fmtVal(ap.pillar_scores.news_catalyst)}</b></div>
        <div className="nx-gauge-box"><span className="muted small">情绪与席位微观分</span><b>{fmtVal(ap.pillar_scores.sentiment_micro)}</b></div>
      </div>

      {/* RAPTOR Real-Time News & Controlled LLM Card */}
      <div className="nx-gauge-box" style={{ marginTop: 10 }} aria-label="RAPTOR实时消息调权与受控大模型状态卡">
        <div className="nx-pick-head">
          <b>RAPTOR 实时消息调权 &amp; 受控大模型 (gpt-6-luna) 状态</b>
          <div className="chips">
            <Badge tone={rtDir.tone}>{rtDir.label}</Badge>
            <Badge tone={Number(rt.tilt_bps) > 0 ? 'good' : Number(rt.tilt_bps) < 0 ? 'bad' : 'neutral'}>
              倾斜 {Number(rt.tilt_bps) > 0 && !String(rt.tilt_bps).startsWith('+') ? `+${rt.tilt_bps}` : rt.tilt_bps} bps
            </Badge>
            {rt.liquidity_filter_triggered && <Badge tone="warn">ACL2026 流动性摩擦过滤已触发</Badge>}
            <Badge tone="neutral">
              模型：{rt.llm_model_used ?? '未启用'}（{rt.llm_status}）
            </Badge>
          </div>
        </div>
        <div className="small">{rt.guard_message}</div>
        <div className="small muted">持有期策略提示：{rt.holding_plan}</div>
        {(rt.bull_points.length > 0 || rt.bear_points.length > 0) && (
          <div className="nx-grid-2">
            <div className="small">
              <b>多头论据：</b>{rt.bull_points.length ? rt.bull_points.join('；') : '暂无'}
            </div>
            <div className="small">
              <b>空头风控：</b>{rt.bear_points.length ? rt.bear_points.join('；') : '暂无'}
            </div>
          </div>
        )}
        {rt.risk_veto_reasons.length > 0 && (
          <div className="notice notice-warn">
            <b>实时消息否决项：</b>{rt.risk_veto_reasons.join(' ； ')}
          </div>
        )}
      </div>

      {ap.risk_vetoes.length > 0 && (
        <div className="notice notice-warn" style={{ marginTop: 8 }}>
          <b>风控一票否决预警：</b> {ap.risk_vetoes.join(' ； ')}
        </div>
      )}

      <ul className="plain small" style={{ marginTop: 8 }}>
        {ap.rationale.map((r, i) => <li key={i}>• {r}</li>)}
      </ul>
    </section>
  );
}

export function NexusTvProSection({ pro }: { pro: NexusStockProAnalysis }) {
  const [selectedTf, setSelectedTf] = useState<'1d' | '1w' | '1mo' | '1y'>('1d');
  const [pivotMethod, setPivotMethod] = useState<PivotLevelSet['method']>('traditional');
  const activeRating = pro.multi_timeframe_ratings.find((r) => r.timeframe === selectedTf) ?? pro.multi_timeframe_ratings[0];
  const activePivot = pro.pivot_points.find((p) => p.method === pivotMethod) ?? pro.pivot_points[0];
  const ps = pro.pro_signals;
  const macdDiv = fmtDivLabel(ps.macd_divergence);
  const rsiDiv = fmtDivLabel(ps.rsi_divergence);

  return (
    <section className="card" aria-label="TradingView专业多周期技术面与筹码结构">
      <SectionTitle
        title="全周期专业技术面（TradingView 26指标评级 · 6大枢轴点 · VPVR筹码峰）"
        hint="严格按 TradingView 官方 15均线 + 11震荡指标等权合成公式计算；样本不足指标保持缺失，绝不补 0。"
        right={(
          <Badge tone={TV_LABEL_TONE[pro.mtf_resonance_label]}>
            日/周/月/年四周期共振：{TV_LABEL_TEXT[pro.mtf_resonance_label]}（{fmtVal(pro.mtf_resonance_score)}）
          </Badge>
        )}
      />

      {/* Multi-timeframe rating cards */}
      <div className="nx-grid-4">
        {pro.multi_timeframe_ratings.map((rt) => (
          <div
            key={rt.timeframe}
            className="nx-gauge-box"
            style={{ outline: rt.timeframe === selectedTf ? '2px solid var(--accent)' : 'none', cursor: 'pointer' }}
            onClick={() => setSelectedTf(rt.timeframe)}
          >
            <div className="nx-pick-head">
              <b>{TF_NAME[rt.timeframe] ?? rt.timeframe}</b>
              <Badge tone={TV_LABEL_TONE[rt.summary.label]}>{TV_LABEL_TEXT[rt.summary.label]}</Badge>
            </div>
            <div className="nx-meter" title={`综合得分 ${fmtVal(rt.summary.score)}`}>
              <span className="nx-meter-needle" style={{ left: needleLeftPct(rt.summary.score) }} />
            </div>
            <div className="small muted">
              买 {rt.summary.buy_count} · 中 {rt.summary.neutral_count} · 卖 {rt.summary.sell_count}（样本 {rt.bar_count} 根{rt.last_bar_date ? ` · ${rt.last_bar_date}` : ''}）
            </div>
          </div>
        ))}
      </div>

      {/* Pro Structural Signals Strip */}
      <div className="chips" style={{ marginTop: 10 }}>
        <Badge tone={ps.supertrend_dir === 'bullish' ? 'good' : ps.supertrend_dir === 'bearish' ? 'bad' : 'neutral'}>
          SuperTrend：{ps.supertrend_dir === 'bullish' ? `多头护航 (${fmtVal(ps.supertrend_level)})` : ps.supertrend_dir === 'bearish' ? `空头压制 (${fmtVal(ps.supertrend_level)})` : '预热缺失'}
        </Badge>
        <Badge tone={ps.bb_kc_squeeze_on ? 'warn' : ps.volatility_squeeze === 'squeeze_fired_up' ? 'good' : ps.volatility_squeeze === 'squeeze_fired_down' ? 'bad' : 'neutral'}>
          波动率挤压(BB-KC)：{SQUEEZE_LABEL[ps.volatility_squeeze] ?? ps.volatility_squeeze}（动量 {fmtVal(ps.squeeze_momentum)}）
        </Badge>
        <Badge tone="neutral">20日锚定VWAP：{fmtVal(ps.anchored_vwap_20d)}</Badge>
        <Badge tone="neutral">相对量比 RVOL(20)：{fmtVal(ps.rvol_20d, 'x')}</Badge>
        <Badge tone="neutral">Wilder ADX(14)：{fmtVal(ps.adx_14)}</Badge>
        <Badge tone="neutral">ATR(14)：{fmtVal(ps.atr_14)}</Badge>
        {macdDiv && <Badge tone={macdDiv.tone}>MACD {macdDiv.text}</Badge>}
        {rsiDiv && <Badge tone={rsiDiv.tone}>RSI {rsiDiv.text}</Badge>}
        {ps.volume_patterns.map((vp) => <Badge key={vp} tone="neutral">量能：{vp}</Badge>)}
      </div>

      <div className="nx-grid-2" style={{ marginTop: 12 }}>
        {/* VPVR Visible Range Volume Profile */}
        <div className="nx-gauge-box">
          <div className="nx-pick-head">
            <b>VPVR 筹码分布与 70% 价值区（{pro.vpvr.lookback_bars} 日窗口）</b>
            {pro.vpvr.winner_ratio !== null
              ? <Badge tone="neutral">获利盘 {(Number(pro.vpvr.winner_ratio) * 100).toFixed(1)}%</Badge>
              : <Badge tone="warn">获利盘 缺失</Badge>}
          </div>
          {!pro.vpvr.available ? (
            <EmptyState>K线样本不足，暂不绘制筹码峰。</EmptyState>
          ) : (
            <>
              <div className="small muted">
                控制点 POC <b className="mono">{fmtVal(pro.vpvr.poc)}</b> · 价值区 [VAL <b className="mono">{fmtVal(pro.vpvr.val)}</b>, VAH <b className="mono">{fmtVal(pro.vpvr.vah)}</b>] · 平均成本 <span className="mono">{fmtVal(pro.vpvr.avg_chip_cost)}</span> · 90%集中度 <span className="mono">{pro.vpvr.concentration_90 !== null ? `${(Number(pro.vpvr.concentration_90) * 100).toFixed(1)}%` : '缺失'}</span>
              </div>
              {(pro.vpvr.hvn_zones.length > 0 || pro.vpvr.lvn_zones.length > 0) && (
                <div className="chips">
                  {pro.vpvr.hvn_zones.map((z) => <Badge key={`hvn:${z}`} tone="neutral">HVN支撑密区 {z}</Badge>)}
                  {pro.vpvr.lvn_zones.map((z) => <Badge key={`lvn:${z}`} tone="warn">LVN真空快区 {z}</Badge>)}
                </div>
              )}
              <div className="nx-vpvr-list" aria-label="VPVR筹码柱状分布">
                {[...pro.vpvr.bins].reverse().filter((_, i) => i % 2 === 0 || pro.vpvr.bins.length <= 14).map((b, idx) => {
                  const sharePct = Number(b.volume_share) * 100;
                  const pctWidth = Number.isFinite(sharePct) ? Math.min(100, sharePct * 5.5) : 0;
                  return (
                    <div key={`${idx}-${b.low}`} className="nx-vpvr-row">
                      <span className="mono">{b.low}~{b.high}</span>
                      <div className="nx-vpvr-track">
                        <div className={`nx-vpvr-fill ${b.is_poc ? 'poc' : b.is_value_area ? 'va' : ''}`} style={{ width: `${pctWidth}%` }} />
                      </div>
                      <span className="mono">
                        {Number.isFinite(sharePct) ? `${sharePct.toFixed(1)}%` : '缺失'} {b.is_poc ? 'POC' : b.node_kind === 'hvn' ? 'HVN' : b.node_kind === 'lvn' ? 'LVN' : ''}
                      </span>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>

        {/* 6 Pivot Point Systems */}
        <div className="nx-gauge-box">
          <div className="nx-pick-head">
            <b>六大经典枢轴点支撑阻力位</b>
            <select value={pivotMethod} onChange={(e) => setPivotMethod(e.target.value as PivotLevelSet['method'])} aria-label="枢轴点算法">
              {Object.entries(PIVOT_NAME).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          </div>
          {activePivot ? (
            <table className="table small">
              <thead><tr><th>阻力层级</th><th>价格</th><th>支撑层级</th><th>价格</th></tr></thead>
              <tbody>
                <tr><td>R4 极强阻力</td><td className="mono nx-up">{fmtVal(activePivot.r4)}</td><td>S1 第一支撑</td><td className="mono nx-down">{fmtVal(activePivot.s1)}</td></tr>
                <tr><td>R3 强阻力</td><td className="mono nx-up">{fmtVal(activePivot.r3)}</td><td>S2 第二支撑</td><td className="mono nx-down">{fmtVal(activePivot.s2)}</td></tr>
                <tr><td>R2 第二阻力</td><td className="mono nx-up">{fmtVal(activePivot.r2)}</td><td>S3 强支撑</td><td className="mono nx-down">{fmtVal(activePivot.s3)}</td></tr>
                <tr><td>R1 第一阻力</td><td className="mono nx-up">{fmtVal(activePivot.r1)}</td><td>S4 极强支撑</td><td className="mono nx-down">{fmtVal(activePivot.s4)}</td></tr>
                <tr><td colSpan={2}><b>中枢轴点 Pivot (P)</b></td><td colSpan={2}><b className="mono">{fmtVal(activePivot.p)}</b></td></tr>
              </tbody>
            </table>
          ) : <MissingText>无枢轴数据</MissingText>}
        </div>
      </div>

      {/* Expandable 26-Indicator Detail Table: 15 MAs + 11 Oscillators */}
      {activeRating && (
        <details className="small" style={{ marginTop: 10 }}>
          <summary>
            查看 {TF_NAME[activeRating.timeframe] ?? activeRating.timeframe} 全部 {activeRating.summary.total_count} 项均线与震荡指标逐票明细
            （均线组 {TV_LABEL_TEXT[activeRating.moving_averages.label]} {activeRating.moving_averages.buy_count}/{activeRating.moving_averages.neutral_count}/{activeRating.moving_averages.sell_count} · 震荡组 {TV_LABEL_TEXT[activeRating.oscillators.label]} {activeRating.oscillators.buy_count}/{activeRating.oscillators.neutral_count}/{activeRating.oscillators.sell_count}）
          </summary>
          <table className="table small" style={{ marginTop: 6 }}>
            <thead><tr><th>指标名称</th><th>类别</th><th>当前数值</th><th>单票判定</th></tr></thead>
            <tbody>
              {[
                ...activeRating.moving_averages.items.map((it) => ({ ...it, group: '移动均线 (15)' })),
                ...activeRating.oscillators.items.map((it) => ({ ...it, group: '震荡指标 (11)' })),
              ].map((ind) => (
                <tr key={`${ind.group}:${ind.name}`}>
                  <td>{ind.name}</td>
                  <td>{ind.group}</td>
                  <td className="mono">{fmtVal(ind.value)}</td>
                  <td>
                    <Badge tone={ind.signal === 'buy' ? 'good' : ind.signal === 'sell' ? 'bad' : ind.signal === 'unavailable' ? 'warn' : 'neutral'}>
                      {ind.signal === 'buy' ? '买入 (+1)' : ind.signal === 'sell' ? '卖出 (-1)' : ind.signal === 'neutral' ? '中性 (0)' : '预热/缺失'}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </section>
  );
}

export function NexusExecutionSection({
  instrumentId,
  releaseId,
  initialPro,
}: {
  instrumentId: string;
  releaseId: string | null;
  initialPro: NexusStockProAnalysis;
}) {
  const [baseShares, setBaseShares] = useState(1000);
  const [availableCash, setAvailableCash] = useState(50000);
  const [gridMode, setGridMode] = useState<GridMode>('atr_adaptive');

  const isCustom = baseShares !== 1000 || availableCash !== 50000 || gridMode !== 'atr_adaptive';
  const customQ = useQuery({
    queryKey: ['bf-nexus', 'stock-pro-exec', instrumentId, releaseId, baseShares, availableCash, gridMode],
    queryFn: ({ signal }) =>
      nexusOps.stockPro(
        instrumentId,
        {
          release_id: releaseId ?? undefined,
          base_shares: baseShares,
          available_cash: availableCash,
          grid_mode: gridMode,
        },
        signal,
      ),
    enabled: isCustom,
    retry: false,
  });

  const active = customQ.data?.data ?? initialPro;
  const tPlan = active.intraday_t_plan;
  const gPlan = active.grid_plan;
  const tCrumble = CRUMBLE_BADGE[tPlan.crumble_risk_level] ?? CRUMBLE_BADGE.unknown;
  const gCrumble = CRUMBLE_BADGE[gPlan.crumble_risk_level] ?? CRUMBLE_BADGE.unknown;

  return (
    <section className="card" aria-label="持仓做T与网格降本交易系统">
      <SectionTitle
        title="个股精细操作：底仓日内做T（ArchetypeTrader）与自适应网格降本系统"
        hint="严格遵守 A股 T+1 老仓可卖、100股整手门槛与卖出万分之5印花税统一成本，集成 ICAIF 2026 神经符号护栏与零前视回测。"
      />

      <div className="filters">
        <label className="select-label">
          可卖老底仓（股）
          <input
            type="number"
            step={100}
            min={0}
            value={baseShares}
            onChange={(e) => {
              const v = Number(e.target.value);
              setBaseShares(Number.isFinite(v) ? Math.max(0, Math.trunc(v)) : 0);
            }}
          />
        </label>
        <label className="select-label">
          机动可用现金（元）
          <input
            type="number"
            step={5000}
            min={0}
            value={availableCash}
            onChange={(e) => {
              const v = Number(e.target.value);
              setAvailableCash(Number.isFinite(v) ? Math.max(0, v) : 0);
            }}
          />
        </label>
        <label className="select-label">
          网格间距算法
          <select value={gridMode} onChange={(e) => setGridMode(e.target.value as GridMode)} aria-label="网格算法">
            <option value="atr_adaptive">ATR(14) 波动率自适应网格（推荐）</option>
            <option value="geometric">Geometric 等比百分比网格</option>
            <option value="arithmetic">Arithmetic 等差价差网格</option>
          </select>
        </label>
        {customQ.isFetching && <span className="muted small">重新测算挂单阶梯…</span>}
      </div>

      <div className="nx-grid-2">
        {/* Intraday-T Card */}
        <div className="nx-gauge-box">
          <div className="nx-pick-head">
            <b>日内做T决策（{tPlan.paper_ref}）</b>
            <div className="chips">
              <Badge tone={tPlan.recommended_direction === 'unavailable' || tPlan.recommended_direction === 'exit_only' ? 'warn' : 'good'}>
                {T_DIR_LABEL[tPlan.recommended_direction] ?? tPlan.recommended_direction}
              </Badge>
              <Badge tone={tCrumble.tone}>{tCrumble.label}</Badge>
            </div>
          </div>
          <div className="small"><b>原型战法：</b>{tPlan.archetype_name}</div>

          <dl className="nx-price-strip">
            <div><dt>推荐单笔股数</dt><dd>{tPlan.recommended_shares} 股（{tPlan.recommended_lots} 手）</dd></div>
            <div><dt>A-S 库存保留价</dt><dd>{fmtVal(tPlan.reservation_price)}</dd></div>
            <div><dt>正T低吸买点</dt><dd className="nx-up">{fmtVal(tPlan.forward_buy_price)}</dd></div>
            <div><dt>正T冲高卖点</dt><dd className="nx-down">{fmtVal(tPlan.forward_sell_price)}</dd></div>
            <div><dt>倒T先卖价</dt><dd className="nx-down">{fmtVal(tPlan.reverse_sell_price)}</dd></div>
            <div><dt>倒T接回价</dt><dd className="nx-up">{fmtVal(tPlan.reverse_buyback_price)}</dd></div>
            <div><dt>放弃做T止损位</dt><dd>{fmtVal(tPlan.abort_stop_price)}</dd></div>
            <div><dt>往返全成本 / 降本</dt><dd>{tPlan.round_trip_fee_bps} bps / {fmtVal(tPlan.expected_cost_reduction_pct, '%')}</dd></div>
          </dl>

          <div className="chips">
            <Badge tone={tPlan.neurosymbolic_guards.all_passed ? 'good' : 'warn'}>
              ICAIF护栏 {tPlan.neurosymbolic_guards.all_passed ? '全部通过' : `拦截 ${tPlan.neurosymbolic_guards.violations.length} 项`}
            </Badge>
            <Badge tone="neutral">价差/成本倍数 {fmtVal(tPlan.neurosymbolic_guards.spread_to_cost_multiple, 'x')}</Badge>
            {tPlan.t1_lock_verified && <Badge tone="good">T+1 锁仓校验</Badge>}
            {tPlan.cash_non_overdraft_verified && <Badge tone="good">现金不透支</Badge>}
          </div>

          <div className="small muted">
            近 {tPlan.backtest_summary.sessions_tested} 日零前视回测：触发 <b>{tPlan.backtest_summary.triggered_days}</b> 日 · 胜率 <b>{fmtVal(tPlan.backtest_summary.win_rate, '%')}</b> · 累计增厚 <b>{fmtVal(tPlan.backtest_summary.cum_extra_return_pct, '%')}</b>（vs 躺平 {fmtVal(tPlan.backtest_summary.vs_hold_delta_pct, '%')}）
          </div>

          <details className="small">
            <summary>查看 ICAIF 2026 神经符号护栏明细（{tPlan.neurosymbolic_guards.checks.length} 项）</summary>
            <ul className="plain small" style={{ marginTop: 4 }}>
              {tPlan.neurosymbolic_guards.checks.map((c) => (
                <li key={c.guard_id}>
                  <Badge tone={c.passed ? 'good' : 'bad'}>{c.passed ? 'PASS' : 'FAIL'}</Badge>{' '}
                  <span className="mono">{c.guard_id}</span>：{c.detail}
                </li>
              ))}
            </ul>
          </details>

          <ul className="plain small">
            {tPlan.execution_notes.map((n, idx) => <li key={idx}>• {n}</li>)}
          </ul>
        </div>

        {/* Adaptive Grid Card */}
        <div className="nx-gauge-box">
          <div className="nx-pick-head">
            <b>自适应网格阶梯（步长 {fmtVal(gPlan.step_pct, '%')} · 每格 {gPlan.per_grid_shares} 股）</b>
            <div className="chips">
              <Badge tone={gPlan.mode === 'budget_insufficient' || gPlan.mode === 'unavailable' ? 'warn' : 'good'}>
                {gPlan.backtest.sessions_tested}日配对 {gPlan.backtest.completed_round_trips} 轮
              </Badge>
              <Badge tone={gCrumble.tone}>{gCrumble.label}</Badge>
            </div>
          </div>

          {gPlan.budget_status_note && (
            <div className={gPlan.mode === 'budget_insufficient' ? 'notice notice-warn small' : 'small muted'}>
              {gPlan.budget_status_note}
            </div>
          )}

          <div className="small muted">
            箱体 [{fmtVal(gPlan.lower_bound)}, {fmtVal(gPlan.upper_bound)}] · 上破熔断 <span className="mono">{fmtVal(gPlan.upper_circuit_breaker)}</span> · 下破风控停机 <span className="mono">{fmtVal(gPlan.lower_stop_breaker)}</span>
          </div>
          <div className="small muted">
            零前视回测：净利 <b>{fmtVal(gPlan.backtest.grid_net_profit_cny, ' 元')}</b> · 每股降本 <b>{fmtVal(gPlan.backtest.cost_reduction_per_share_cny, ' 元')}</b>（{fmtVal(gPlan.backtest.cost_reduction_pct, '%')}）· vs 买入持有 <b>{fmtVal(gPlan.backtest.vs_buy_hold_excess_pct, '%')}</b> · 峰值资金占用 <b>{fmtVal(gPlan.backtest.max_capital_usage_pct, '%')}</b>
          </div>

          {gPlan.rungs.length === 0 ? (
            <EmptyState>当前预算或样本条件暂不生成网格挂单阶梯。</EmptyState>
          ) : (
            <table className="table small">
              <thead><tr><th>档位</th><th>触发价</th><th>方向</th><th>股数</th><th>配对目标价 · 预估净利</th></tr></thead>
              <tbody>
                {[...gPlan.rungs].reverse().map((r) => (
                  <tr key={r.level_index} className={r.action === 'buy_dip' ? 'nx-rung-buy' : r.action === 'sell_rally' ? 'nx-rung-sell' : 'nx-rung-anchor'}>
                    <td className="mono">{r.level_index > 0 ? `+${r.level_index}` : r.level_index}</td>
                    <td className="mono">{r.trigger_price}</td>
                    <td>{r.action === 'buy_dip' ? '分批低吸买单' : r.action === 'sell_rally' ? '分批高抛卖单' : '网格基准锚点'}</td>
                    <td className="mono">{r.shares}</td>
                    <td className="mono">
                      {r.target_pair_price ? `${r.target_pair_price} (${fmtVal(r.est_net_profit_cny, '元')})` : '基准中枢'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </section>
  );
}

export function NexusMicrostructureSection({ pro }: { pro: NexusStockProAnalysis }) {
  const rr = pro.retail_resonance;
  const sb = pro.seat_battle;
  const crowdMeta = CROWD_STATE_TEXT[rr.crowd_state] ?? CROWD_STATE_TEXT.unavailable;

  return (
    <section className="card" aria-label="四时段散户情绪证伪与龙虎榜营业部战法画像">
      <SectionTitle
        title="散户评论四时段共振证伪 &amp; 龙虎榜知名席位对手盘博弈"
        hint="先客观核验股吧/评论区热门说法真伪与水军占比，再结合 17 大席位谱系与四维真伪机构给出对手盘应对策略。"
      />

      {/* 4-Session Guba Discussion & Fact-Check */}
      <div>
        <div className="nx-pick-head">
          <h4>股吧四时段（盘前/盘中/午休/盘后）热度与 SIR 情绪传染监测</h4>
          <div className="chips">
            <Badge tone={crowdMeta.tone}>{crowdMeta.label}</Badge>
            <Badge tone="neutral">总帖数 {rr.total_posts}</Badge>
            <Badge tone="neutral">
              水军重复率 {rr.template_spam_ratio !== null ? `${(Number(rr.template_spam_ratio) * 100).toFixed(1)}%` : '缺失'}
            </Badge>
            <Badge tone="neutral">异常关注 Z={fmtVal(rr.abnormal_attention_z)}</Badge>
            <Badge tone="neutral">SIR 传染斜率={fmtVal(rr.sir_contagion_slope)}</Badge>
          </div>
        </div>

        <div className="nx-session-grid">
          {rr.sessions.map((s) => (
            <div key={s.session_bucket} className="nx-session-card">
              <div className="nx-pick-head">
                <b>{s.label}</b>
                <Badge tone={s.heat_state === 'overheated_warning' ? 'bad' : s.heat_state === 'warming' ? 'warn' : 'neutral'}>
                  {s.post_count} 帖 / {s.unique_authors} 人
                </Badge>
              </div>
              <div className="small muted">
                看多比：{s.bullish_ratio !== null ? `${(Number(s.bullish_ratio) * 100).toFixed(0)}%` : '缺失'} · 分歧度：{fmtVal(s.divergence_index)}
              </div>
              <div className="small">{s.dominant_topic}</div>
            </div>
          ))}
        </div>

        {rr.claim_checks.length > 0 && (
          <div style={{ marginTop: 8 }}>
            <h4>评论区热门观点 · 硬数据交叉证伪</h4>
            <ul className="plain small">
              {rr.claim_checks.map((cv, i) => {
                const b = CLAIM_VERDICT_BADGE[cv.verdict] ?? { label: cv.verdict, tone: 'neutral' as const };
                return (
                  <li key={i}>
                    <Badge tone={b.tone}>{b.label}</Badge> <b>「{cv.retail_claim}」</b> — {cv.objective_evidence}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        <p className="small muted" style={{ marginTop: 6 }}>{rr.synthesized_judgment}</p>
      </div>

      {/* LHB Famous Brokerage Seat Personas */}
      <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
        <div className="nx-pick-head">
          <h4>龙虎榜 17 大营业部谱系战法拆解（含拉萨天团 / 国泰海通更名兼容 / 四维真伪机构识别）</h4>
          <div className="chips">
            <Badge tone="neutral">战局定性：{sb.battle_verdict}</Badge>
            {sb.report_date && <Badge tone="neutral">最近上榜 {sb.report_date}</Badge>}
            {sb.net_buy_amount_cny !== null && <Badge tone="neutral">净买 {fmtMoney(sb.net_buy_amount_cny)} 元</Badge>}
            {sb.lhasa_buyer_count > 0 && (
              <Badge tone="bad">
                拉萨买榜 {sb.lhasa_buyer_count} 席（占比 {sb.lhasa_ratio !== null ? `${(Number(sb.lhasa_ratio) * 100).toFixed(0)}%` : '缺失'}）
              </Badge>
            )}
            {sb.one_day_raider_count > 0 && <Badge tone="bad">一日游砸盘 {sb.one_day_raider_count} 席</Badge>}
            {sb.synergy_leader_count > 0 && <Badge tone="good">格局游资 {sb.synergy_leader_count} 席</Badge>}
            {sb.quant_seat_count > 0 && <Badge tone="warn">量化席位 {sb.quant_seat_count} 席</Badge>}
            {sb.fake_institution_check.institution_present && (
              <Badge tone={sb.fake_institution_check.verdict === 'real_institution_likely' ? 'good' : 'warn'}>
                机构鉴别：{sb.fake_institution_check.verdict === 'real_institution_likely' ? '倾向真机构' : '警惕量化假机构'}（风险 {fmtVal(sb.fake_institution_check.risk_score)}）
              </Badge>
            )}
          </div>
        </div>

        <p className="small">{sb.opponent_strategy}</p>
        {sb.fake_institution_check.reasons.length > 0 && (
          <p className="small muted">四维真伪机构诊断：{sb.fake_institution_check.reasons.join(' ； ')}</p>
        )}

        {sb.seats.length > 0 && (
          <table className="table small" style={{ marginTop: 6 }}>
            <thead><tr><th>方向</th><th>营业部名称</th><th>识别谱系画像</th><th>净额(元)</th><th>实证 D+1 / D+5</th><th>应对策略</th></tr></thead>
            <tbody>
              {sb.seats.map((st, idx) => {
                const badge = SEAT_CATEGORY_BADGE[st.category] ?? SEAT_CATEGORY_BADGE.regular_branch!;
                return (
                  <tr key={idx}>
                    <td><Badge tone={st.side === 'buy' ? 'good' : st.side === 'sell' ? 'bad' : 'warn'}>{st.side === 'buy' ? '买入榜' : st.side === 'sell' ? '卖出榜' : '双向同框'}</Badge></td>
                    <td>{st.seat_name}</td>
                    <td><Badge tone={badge.tone}>{st.persona_label}</Badge></td>
                    <td className="mono">{fmtMoney(st.net_amount_cny)}</td>
                    <td className="mono">
                      {fmtVal(st.empirical_d1_gap_pct, '%')} / {fmtVal(st.empirical_d5_excess_pct, '%')}
                    </td>
                    <td>{st.counter_tactic}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Verified News Section */}
      {pro.verified_news.length > 0 && (
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
          <h4>个股多源交叉核验新闻与公告</h4>
          <ul className="plain small">
            {pro.verified_news.map((nw) => {
              const vb = FLASH_VER_TEXT[nw.verification_state] ?? { label: nw.verification_state, tone: 'neutral' as const };
              return (
                <li key={nw.cluster_id}>
                  <Badge tone={vb.tone}>{vb.label}</Badge>{' '}
                  <Badge tone={nw.polarity === 'positive' ? 'good' : nw.polarity === 'negative' ? 'bad' : 'neutral'}>
                    {nw.polarity === 'positive' ? '利多' : nw.polarity === 'negative' ? '利空' : '中性'}
                  </Badge>{' '}
                  <b>{nw.headline}</b>{' '}
                  <span className="muted">（{nw.sources.join('、')}{nw.published_at ? ` · ${fmtTime(nw.published_at)}` : ''}）</span>
                  <div className="muted">{nw.bull_bear_note}</div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}

/**
 * Standalone full Stock Workbench rendered seamlessly when decision.workspace_projection
 * has no row (covering all ~5,900+ non-top-50 A-share instruments via PostgreSQL cache),
 * complete with a TradingView-grade <PriceChart /> driven by pro.kline_bars.
 */
export function NexusFallbackStockWorkbench({
  pro,
  meta,
  rel,
  horizonKey,
}: {
  pro: NexusStockProAnalysis;
  meta?: Parameters<typeof MetaStrip>[0]['meta'];
  rel: ReleaseCtx;
  horizonKey: string;
}) {
  const theme = useTheme();
  const [showVolume, setShowVolume] = useState(true);
  const [showOverlay, setShowOverlay] = useState(true);

  const barWindow = useMemo(() => buildBarWindowFromNexus(pro), [pro]);
  const chartLines: ChartLine[] = useMemo(() => {
    if (!showOverlay || pro.adjustment_mode !== 'raw') return [];
    const out: ChartLine[] = [];
    for (const nl of pro.chart_lines) {
      const p = toChartNumber(nl.price);
      if (p !== null) out.push({ id: nl.id, price: p, title: nl.title, tone: nl.tone, dashed: nl.dashed });
    }
    return out;
  }, [pro.adjustment_mode, pro.chart_lines, showOverlay]);

  return (
    <div className="page">
      <ReleaseBar rel={rel} displayedReleaseId={pro.release_id} />
      <p><Link to={`/nexus?release=${pro.release_id}`}>← 返回全域选股中枢</Link> · <Link to={`/?h=${horizonKey}`}>返回首页</Link></p>

      <header className="card stock-head">
        <div className="stock-title">
          <h1>{pro.display_name}</h1>
          <span className="muted mono">{pro.symbol}</span>
          <Badge tone="neutral">{pro.board_code ?? 'main'}</Badge>
          <Badge tone="good">全市场覆盖 · PostgreSQL 工作台持久化</Badge>
          <WatchStar instrumentId={pro.instrument_id} releaseId={pro.release_id} />
        </div>
        <dl className="facts">
          <div><dt>分析基准时点</dt><dd>{fmtTime(pro.decision_as_of)}</dd></div>
          <div><dt>发布版本</dt><dd className="mono">{pro.release_id.slice(0, 8)}</dd></div>
          <div><dt>多周期技术共振</dt><dd>{TV_LABEL_TEXT[pro.mtf_resonance_label]}</dd></div>
          <div><dt>VPVR 筹码控制点</dt><dd className="mono">{fmtVal(pro.vpvr.poc)}</dd></div>
        </dl>
        {meta && <MetaStrip meta={meta} title="Nexus 全市场个股工作台（PostgreSQL 缓存贯通）" rows={[['cache_key_hash', pro.cache_key_hash], ['cached_in_postgres', String(pro.cached_in_postgres)]]} />}
      </header>

      {/* TradingView-Grade Candlestick + Volume + Anchor Lines + Pro Signal Markers */}
      <section className="card" aria-label="全市场个股K线与买卖锚点图表">
        <SectionTitle
          title={`K线图与买卖锚点工作台（${pro.timeframe} · ${pro.kline_bars.length} 根）`}
          hint={pro.price_basis_note}
          right={(
            <div className="chips">
              <label className="check"><input type="checkbox" checked={showVolume} onChange={(e) => setShowVolume(e.target.checked)} />成交量</label>
              <label className="check"><input type="checkbox" checked={showOverlay} onChange={(e) => setShowOverlay(e.target.checked)} />买卖锚点与信号</label>
            </div>
          )}
        />
        {pro.adjustment_mode !== 'raw' && (
          <div className="notice">复权价只用于看走势与技术面，买卖区间、做T与网格挂单仍按原始可交易价计算，锚点价位线已在复权图上自动隐藏。</div>
        )}
        {barWindow.bars.length === 0 ? (
          <EmptyState>当前标的暂无历史K线样本。</EmptyState>
        ) : (
          <PriceChart
            bars={barWindow}
            showVolume={showVolume}
            lines={chartLines}
            drawMode={false}
            themeVersion={theme.version}
            markers={showOverlay ? pro.chart_markers : undefined}
          />
        )}
      </section>

      <NexusActionSummaryCard pro={pro} />
      <NexusTvProSection pro={pro} />
      <NexusExecutionSection instrumentId={pro.instrument_id} releaseId={pro.release_id} initialPro={pro} />
      <NexusMicrostructureSection pro={pro} />
    </div>
  );
}
