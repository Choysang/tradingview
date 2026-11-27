/**
 * Nexus 2026 Unified Stock Selection & Five-Dimension Intelligence Hub (/nexus).
 *
 * Unifies:
 * 1. Multi-horizon Orthogonal Ensemble Stock Recommendation Leaderboard ('取其精华、去其糟粕',
 *    normalized across ALL active voters so no thin specialist inflates scores).
 * 2. Rapidly Rising Sectors & Early Candidate Watch Pool ('火速晋升板块 · 备选池抢先锁定').
 * 3. Active RAPTOR Real-Time News & Controlled LLM (gpt-6-luna) Adjustments.
 * 4. 5-Dimension A-Share Full-Factor Matrix (Domestic Policy/M1, Global FOMC/FX/A50,
 *    Post-Aug-2024 Northbound Capital Proxies, Retail Sentiment, LHB Microstructure).
 * 5. Multi-Source Cross-Verified News Flashes & Anti-Silent-Source Polling Watchdog.
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Badge } from '../../../components/Badges';
import { stockHref } from '../../../components/Lists';
import { MetaStrip } from '../../../components/MetaStrip';
import { ReleaseBar } from '../../../components/ReleaseBar';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { useReleaseSelection } from '../../../hooks/useRelease';
import { fmtTime } from '../../../lib/format';
import {
  type CrossVerifiedFlash,
  fmtVal,
  type HorizonTrack,
  type MacroFactorItem,
  nexusOps,
  SECTOR_ROLE_TEXT,
} from './api';
import './nexus.css';

const TRACKS: Array<{ key: HorizonTrack; label: string; hint: string }> = [
  { key: 'composite', label: '综合正交总榜', hint: '融合多因子+顶会低波+T+1微观+板块溢出全体投票归一' },
  { key: 'short', label: '短线爆发 (3~5日)', hint: '侧重板块超边溢出、T+1隔夜日内拔河与主力吸筹' },
  { key: 'swing', label: '波段中军 (10~20日)', hint: '侧重一手门槛机构动量、两融/彩票防雷低波与多因子主书' },
  { key: 'long', label: '长线底仓 (20~60日)', hint: '侧重PRISM-VQ离散体制低换手专家与高确定性红利价值' },
];

const DIM_LABEL: Record<MacroFactorItem['dimension'], string> = {
  domestic_policy_macro: '国内政策与新口径M1',
  global_linkage: '国际宏观/汇率/A50映射',
  capital_proxy_2026: '北向停更后高频资金替代',
  retail_sentiment: '股吧四时段散户情绪共振',
  microstructure_seats: '龙虎榜席位与四维假机构微观结构',
};

const SIGNAL_BADGE: Record<MacroFactorItem['signal'], { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  supportive: { label: '偏多支撑', tone: 'good' },
  neutral: { label: '中性均衡', tone: 'neutral' },
  cautious: { label: '谨慎防御', tone: 'warn' },
  unknown: { label: '信号未知', tone: 'neutral' },
};

const STATUS_BADGE: Record<MacroFactorItem['status'], { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  observed: { label: '实测已观测', tone: 'good' },
  partial: { label: '部分指标待同步', tone: 'warn' },
  awaiting_feed: { label: '等待源站首轮入库', tone: 'neutral' },
};

const FLASH_STATE_BADGE: Record<CrossVerifiedFlash['verification_state'], { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  official_confirmed: { label: '官方权威核验', tone: 'good' },
  multi_source_verified: { label: '多源交叉印证', tone: 'good' },
  single_source_flash: { label: '单源快讯待核实', tone: 'warn' },
  contradicted: { label: '多源证伪/澄清', tone: 'bad' },
};

const DIRECTIVE_BADGE: Record<string, { label: string; tone: 'good' | 'warn' | 'bad' | 'neutral' }> = {
  catalyst_boost: { label: '催化正向加权', tone: 'good' },
  tighten_stop_grid: { label: '收紧止损与网格间距', tone: 'warn' },
  risk_veto: { label: '风控一票否决', tone: 'bad' },
  neutral_monitor: { label: '中性观察', tone: 'neutral' },
};

export function NexusHubPage() {
  const rel = useReleaseSelection();
  const rid = rel.effectiveReleaseId;
  const [track, setTrack] = useState<HorizonTrack>('composite');

  const q = useQuery({
    queryKey: ['bf-nexus', 'hub', track, rid ?? 'latest'],
    queryFn: ({ signal }) => nexusOps.hub({ track, limit: 20, ...(rid ? { release_id: rid } : {}) }, signal),
    staleTime: 30_000,
  });

  const d = q.data?.data;

  return (
    <div className="page">
      <ReleaseBar rel={rel} displayedReleaseId={d?.release_id ?? undefined} />

      <SectionTitle
        title="Nexus 全域五维选股与情报总控中枢"
        hint="多源交叉核验消息面 + 五维国内外资金宏观映射 + RAPTOR 实时调权 + 瑞士轮正交冠军策略「取其精华、去其糟粕」排序。点击任意个股直达全周期技术面与做T网格工作台。"
        right={<Link className="btn btn-small" to="/strategies?tab=arena">查看瑞士轮策略天梯 →</Link>}
      />

      {q.isLoading && <Loading label="合成五维因子矩阵与正交选股榜单…" />}
      {q.error && <ErrorState error={q.error} what="全域选股中枢" onRetry={() => void q.refetch()} />}

      {d && (
        <>
          <MetaStrip
            meta={q.data!.meta}
            title="全域选股中枢"
            rows={[
              ['horizon_track', d.horizon_track],
              ['normalization_rule', d.normalization_rule],
              ['arena_weight_source', d.arena_weight_source],
            ]}
          />

          {d.release_id === null && (
            <div className="notice notice-info" role="status">
              <b>等待首次分析发布快照：</b>
              <span>当前数据库尚未生成已发布的分析快照（`release_id = null`），已先行展示宏观五维矩阵、多源防静默轮询与实时调权状态。运行 `analysis daily` 后将自动填充正交推荐榜。</span>
            </div>
          )}

          {/* Regime Banner */}
          <div className="nx-banner" role="region" aria-label="市场总体制与仓位护航">
            <div>
              <b>当前市场体制：{d.market_regime.label}</b>
              <div className="small muted">{d.market_regime.summary}</div>
              <div className="small muted">投票权重来源：<span className="mono">{d.arena_weight_source}</span></div>
            </div>
            <div className="chips">
              <Badge tone={d.market_regime.curve_gate_active ? 'warn' : 'good'}>
                {d.market_regime.curve_gate_active ? '20日净值风控闸门激活 (0.3x降仓)' : '净值均线健康'}
              </Badge>
              <Badge tone="good">建议总仓位上限 {d.market_regime.recommended_exposure_pct}</Badge>
            </div>
          </div>

          {/* 1. Multi-Horizon Orthogonal Stock Recommendation Leaderboard */}
          <section className="card" aria-label="按推荐顺序排列个股总榜">
            <div className="nx-pick-head">
              <h3>精选个股推荐总榜（按正交合成得分严格排序）</h3>
              <div className="segmented" role="tablist" aria-label="选股周期赛道">
                {TRACKS.map((t) => (
                  <button
                    key={t.key}
                    role="tab"
                    aria-selected={track === t.key}
                    className={track === t.key ? 'on' : ''}
                    onClick={() => setTrack(t.key)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </div>
            <p className="small muted">{TRACKS.find((t) => t.key === track)?.hint}</p>

            {d.picks.length === 0 ? (
              <EmptyState>
                {d.release_id === null
                  ? '等待首次分析发布：尚未生成个股推荐榜，请先执行分析发布任务。'
                  : '当前发布快照暂无可评分证券。'}
              </EmptyState>
            ) : (
              <div className="nx-grid-2">
                {d.picks.map((p) => (
                  <article key={p.instrument_id} className="nx-pick-card">
                    <div className="nx-pick-head">
                      <div className="chips">
                        <span className="nx-rank-pill">#{p.rank}</span>
                        <Link to={stockHref(p.instrument_id, d.release_id)}>
                          <b>{p.display_name}</b> <span className="mono muted">{p.symbol}</span>
                        </Link>
                        {p.sector_node && (
                          <Link
                            className="badge badge-neutral"
                            to={`/sector/${encodeURIComponent(p.sector_node)}${d.release_id ? `?release=${d.release_id}` : ''}`}
                          >
                            {p.sector_node} · {SECTOR_ROLE_TEXT[p.sector_role] ?? p.sector_role}
                          </Link>
                        )}
                        {p.tags.map((tg) => <Badge key={tg} tone="neutral">{tg}</Badge>)}
                        {p.risk_flags.map((rf) => <Badge key={rf} tone="warn">{rf}</Badge>)}
                      </div>
                      <Badge tone="good">合成得分 {p.ensemble_score}</Badge>
                    </div>

                    <dl className="nx-price-strip">
                      <div><dt>低吸买入区间</dt><dd>{fmtVal(p.entry_low)} ~ {fmtVal(p.entry_high)}</dd></div>
                      <div><dt>纪律止损线</dt><dd className="nx-down">{fmtVal(p.stop_loss)}</dd></div>
                      <div><dt>第一目标位</dt><dd className="nx-up">{fmtVal(p.target_price)}</dd></div>
                      <div><dt>持有计划</dt><dd>{p.holding_horizon}</dd></div>
                    </dl>

                    <div className="small muted">
                      {p.catalyst_summary}（投票共振 <span className="mono">{p.voter_support_count}/{p.total_active_voters}</span>）
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>

          {/* 2. Rapidly Rising Sectors & Early Watch Pool */}
          <section className="card" aria-label="火速晋升板块与提前备选池">
            <SectionTitle
              title="🔥 火速晋升板块 & 优秀个股提前备选池"
              hint="监控相对强弱跃迁、换手加速度与领涨股确认度，在板块主升爆发早期将前排优秀个股提前纳入备选池。"
            />
            {d.rapid_rising_sectors.length === 0 ? (
              <EmptyState>当前发布暂无板块升温排名记录，点击上方板块标签仍可直接查看板块内 Top 推荐个股。</EmptyState>
            ) : (
              <div className="nx-grid-3">
                {d.rapid_rising_sectors.map((s) => (
                  <div key={`${s.taxonomy_id}:${s.node_id}`} className="nx-gauge-box">
                    <div className="nx-pick-head">
                      <Link to={`/sector/${encodeURIComponent(s.node_id)}?${d.release_id ? `release=${d.release_id}&` : ''}tax=${encodeURIComponent(s.taxonomy_id)}`}>
                        <b>#{s.rank} {s.node_id}</b>
                      </Link>
                      <Badge tone={s.velocity_label === 'rapid_promotion' ? 'good' : 'neutral'}>
                        升温分 {s.surge_score}
                      </Badge>
                    </div>
                    <div className="small muted">
                      成分 {s.member_count} 只 · 已就绪 {s.present_components.length} 项 · 缺失项：{s.missing_components.length ? s.missing_components.join('、') : '无'}
                    </div>
                    {s.watch_candidates.length > 0 && (
                      <div className="chips">
                        {s.watch_candidates.map((w) => (
                          <Link key={w.instrument_id} className="chip" to={stockHref(w.instrument_id, d.release_id)}>
                            {w.display_name} <span className="mono muted">{w.symbol}</span>
                          </Link>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {d.candidate_watch_pool.length > 0 && (
              <div style={{ marginTop: 10 }}>
                <h4>备选池重点跟踪名单（{d.candidate_watch_pool.length} 只）</h4>
                <div className="chips">
                  {d.candidate_watch_pool.map((c) => (
                    <Link key={c.instrument_id} className="chip" to={stockHref(c.instrument_id, d.release_id)} title={c.entry_hint}>
                      <b>{c.display_name}</b> <span className="mono muted">{c.symbol}</span>
                      <Badge tone={c.pool_state === 'actionable' ? 'good' : 'warn'}>{c.sector_node} · {c.pool_state}</Badge>
                    </Link>
                  ))}
                </div>
              </div>
            )}
          </section>

          {/* 3. Active RAPTOR Real-Time Adjustments & Controlled gpt-6-luna Lane */}
          <section className="card" aria-label="RAPTOR实时消息调权与受控大模型状态">
            <SectionTitle
              title="⚡ RAPTOR 实时消息面调权 & 受控大模型 (gpt-6-luna) 护栏"
              hint="仅对已通过确定性验证的候选证券在 +-15% (1500 bps) 安全边界内执行消息面微调或风控收紧；受控模型仅允许 gpt-6-luna。"
            />
            {d.active_realtime_adjustments.length === 0 ? (
              <EmptyState>当前窗口暂无生效的 RAPTOR 实时调权事件，保持盘前正交基准排序。</EmptyState>
            ) : (
              <div className="nx-grid-2">
                {d.active_realtime_adjustments.map((adj) => {
                  const dirMeta = DIRECTIVE_BADGE[adj.action_directive] ?? { label: adj.action_directive, tone: 'neutral' as const };
                  return (
                    <div key={adj.adjustment_id} className="nx-gauge-box">
                      <div className="nx-pick-head">
                        <div className="chips">
                          <Badge tone="neutral">{adj.target_scope}</Badge>
                          <b>{adj.target_key}</b>
                          <Badge tone={dirMeta.tone}>{dirMeta.label}</Badge>
                        </div>
                        <Badge tone={Number(adj.tilt_bps) > 0 ? 'good' : Number(adj.tilt_bps) < 0 ? 'bad' : 'neutral'}>
                          {Number(adj.tilt_bps) > 0 && !String(adj.tilt_bps).startsWith('+') ? `+${adj.tilt_bps}` : adj.tilt_bps} bps
                        </Badge>
                      </div>
                      <div className="small">{adj.summary}</div>
                      <div className="small muted">
                        模型通道：<span className="mono">{adj.llm_model_used ?? '确定性规则引擎（未触发 LLM）'}</span> · 生效时间：{adj.available_at ? fmtTime(adj.available_at) : '即时'}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* 4. 5D A-Share Domestic + Global + Capital Factor Matrix */}
          <section className="card" aria-label="A股五维全因子影响矩阵">
            <SectionTitle
              title="A股国内外五维全因子传导矩阵（含2025新口径M1与北向停更五大高频替代）"
              hint="每一项因子均绑定 Tier-1/2 权威数据源与实证台账锚点，未同步字段明确标出状态，不做主观臆测。"
            />
            <div className="nx-grid-3">
              {d.macro_factor_matrix.map((f) => {
                const sig = SIGNAL_BADGE[f.signal] ?? SIGNAL_BADGE.unknown;
                const st = STATUS_BADGE[f.status] ?? STATUS_BADGE.partial;
                return (
                  <div key={`${f.dimension}:${f.factor_name}`} className="nx-gauge-box">
                    <div className="nx-pick-head">
                      <b>{f.factor_name}</b>
                      <div className="chips">
                        <Badge tone={sig.tone}>{sig.label}</Badge>
                        <Badge tone={st.tone}>{st.label}</Badge>
                      </div>
                    </div>
                    <div className="small muted">{DIM_LABEL[f.dimension] ?? f.dimension}</div>
                    <div className="small">{f.detail}</div>
                    <div className="small muted">权威信源：{f.source_authority}</div>
                  </div>
                );
              })}
            </div>
          </section>

          {/* 5. Multi-Source Cross-Verified Flashes & Polling Watchdog */}
          <div className="nx-grid-2">
            <section className="card" aria-label="多源交叉核验实时电报">
              <SectionTitle
                title="新华社 / 巨潮 / 财联社 / 东财 多源交叉核验快讯"
                hint="Tier-1 官方源定锚，单源未证实传闻自动降权隔离（E088 标题级非对称极性规则）。"
              />
              {d.cross_verified_flashes.length === 0 ? (
                <EmptyState>当前窗口暂无已入库交叉核验快讯（运行 python -m pulse.workers.nexus poll-intel 拉取）。</EmptyState>
              ) : (
                <ul className="plain small">
                  {d.cross_verified_flashes.map((fl) => {
                    const vs = FLASH_STATE_BADGE[fl.verification_state] ?? { label: fl.verification_state, tone: 'neutral' as const };
                    return (
                      <li key={fl.cluster_id}>
                        <Badge tone={vs.tone}>
                          {fl.verification_state === 'multi_source_verified' ? `${fl.corroboration_count}源交叉印证` : vs.label}
                        </Badge>{' '}
                        <Badge tone={fl.polarity === 'positive' ? 'good' : fl.polarity === 'negative' ? 'bad' : 'neutral'}>
                          {fl.polarity === 'positive' ? '利多' : fl.polarity === 'negative' ? '利空预警' : '中性'}
                        </Badge>{' '}
                        <b>{fl.headline}</b>{' '}
                        <span className="muted">（来源：{fl.sources.join('、')} · 置信度 {fl.confidence_score}{fl.published_at ? ` · ${fmtTime(fl.published_at)}` : ''}）</span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>

            <section className="card" aria-label="多信息源防静默轮询健康监控">
              <SectionTitle
                title="多信息源轮询与防静默看门狗状态"
                hint="严格执行单域名 <=1 req/s 限速、游标 skip 绝不提前 break、以及 HTTP 200 空列表静默告警。"
              />
              <div className="chips" style={{ marginBottom: 8 }}>
                <Badge tone="neutral">注册总源 {d.source_polling_status.total_sources}</Badge>
                <Badge tone="good">健康 {d.source_polling_status.healthy_count}</Badge>
                <Badge tone={d.source_polling_status.degraded_count > 0 ? 'warn' : 'neutral'}>
                  降级/待首轮 {d.source_polling_status.degraded_count}
                </Badge>
                <Badge tone={d.source_polling_status.silent_warning_count > 0 ? 'bad' : 'good'}>
                  静默告警 {d.source_polling_status.silent_warning_count}
                </Badge>
                <Badge tone="neutral">限速 &lt;= {d.source_polling_status.host_rate_limit_rps} rps</Badge>
              </div>
              <table className="table small">
                <thead><tr><th>信息源</th><th>权威层级</th><th>运行 / 新鲜度</th><th>最近产出 · 容灾组</th></tr></thead>
                <tbody>
                  {d.source_polling_status.sources.map((s) => (
                    <tr key={s.source_id}>
                      <td><b className="mono">{s.source_id}</b></td>
                      <td>{s.tier === 'tier1_official' ? 'Tier-1 官方' : s.tier === 'tier2_financial_press' ? 'Tier-2 持牌电报' : 'Tier-3 门户社区'}</td>
                      <td>
                        <div className="chips">
                          <Badge tone={s.runtime_state === 'healthy' ? 'good' : s.runtime_state === 'awaiting_first_poll' ? 'neutral' : 'warn'}>
                            {s.runtime_state}
                          </Badge>
                          <Badge tone={s.freshness_state === 'current' ? 'good' : s.freshness_state === 'stale' ? 'bad' : 'neutral'}>
                            {s.freshness_state}
                          </Badge>
                        </div>
                      </td>
                      <td className="small muted">
                        {s.last_success_at ? fmtTime(s.last_success_at) : '缺失'} · <span className="mono">{s.fallback_group}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
