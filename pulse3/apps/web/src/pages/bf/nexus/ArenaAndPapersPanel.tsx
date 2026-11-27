/**
 * Strategy Taxonomy Router, Swiss-System Glicko-2 Arena, AlphaPROBE Candidates,
 * 2024–2026 Top-Venue Papers, and Unified GIPS-AShare-2026-v1 Accounting Benchmark Parity Auditor (Nexus 3.5).
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Badge } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { fmtTime } from '../../../lib/format';
import {
  DISCOVERED_GATE_TEXT,
  fmtVal,
  HORIZON_BUCKET_TEXT,
  LAYER_TEXT,
  LIFECYCLE_TIER_TEXT,
  nexusOps,
  PARADIGM_TEXT,
  type StrategyTaxonomyEntry,
} from './api';
import './nexus.css';

export function SwissArenaView() {
  const [layer, setLayer] = useState('');
  const [horizonBucket, setHorizonBucket] = useState('');
  const [paradigm, setParadigm] = useState('');
  const [role, setRole] = useState('');

  const q = useQuery({
    queryKey: ['bf-nexus', 'arena', layer, horizonBucket, paradigm, role],
    queryFn: ({ signal }) =>
      nexusOps.arena(
        {
          ...(layer ? { layer } : {}),
          ...(horizonBucket ? { horizon_bucket: horizonBucket } : {}),
          ...(paradigm ? { paradigm } : {}),
          ...(role ? { role } : {}),
        },
        signal,
      ),
    staleTime: 60_000,
  });

  if (q.isLoading) return <Loading label="读取瑞士轮 Glicko-2 策略天梯与多维分类路由…" />;
  if (q.error) return <ErrorState error={q.error} what="瑞士轮策略天梯" onRetry={() => void q.refetch()} />;
  if (!q.data) return null;

  const d = q.data.data;
  const champions = d.strategies.filter((s) => s.is_cluster_champion);

  return (
    <section aria-label="瑞士轮策略天梯与多维分类路由">
      <div className="nx-banner">
        <div>
          <b>赛季 {d.season_id} · 瑞士轮优胜劣汰闭环（已完成 {d.rounds_completed} 轮 · 评估 {d.sessions_evaluated} 个交易日）</b>
          <div className="small muted">
            纳管策略 <b>{d.total_strategies}</b> 套 · 正交聚类簇 <b>{d.orthogonal_clusters}</b> 个 · 记账基准 <span className="mono">{d.benchmark_standard.standard_id}</span> · 评估来源 <span className="mono">{d.evaluation_basis}</span>
          </div>
          <div className="small muted">验证协议：<span className="mono">{d.benchmark_standard.validation_protocol}</span></div>
        </div>
        <Badge tone="good">Glicko-2 保守下界 (μ - 2φ) + RAM 保底分配</Badge>
      </div>

      {/* Multi-dimensional Taxonomy Filter Router */}
      <div className="filters" style={{ marginTop: 10 }}>
        <label className="select-label">
          决策层级 (Layer)
          <select value={layer} onChange={(e) => setLayer(e.target.value)} aria-label="决策层级">
            <option value="">全部层级</option>
            {Object.entries(LAYER_TEXT).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="select-label">
          持仓期限 (Horizon)
          <select value={horizonBucket} onChange={(e) => setHorizonBucket(e.target.value)} aria-label="持仓期限">
            <option value="">全部周期</option>
            {Object.entries(HORIZON_BUCKET_TEXT).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="select-label">
          方法流派 (Paradigm)
          <select value={paradigm} onChange={(e) => setParadigm(e.target.value)} aria-label="方法流派">
            <option value="">全部流派</option>
            {Object.entries(PARADIGM_TEXT).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="select-label">
          组合角色 (Role)
          <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="组合角色">
            <option value="">全部角色</option>
            <option value="core_alpha">核心进攻 Alpha</option>
            <option value="defensive_shield">抗回撤防御盾</option>
            <option value="cost_reducer">持仓做T/网格降本</option>
            <option value="risk_veto">风控一票否决</option>
            <option value="exploratory_shadow">影子探索验证</option>
          </select>
        </label>
      </div>

      {/* Orthogonal Cluster Champions (derived from strategies.filter(is_cluster_champion)) */}
      <h3>正交相关性簇冠军（|Corr| &lt; 0.70 入选实盘合成 · {champions.length} 席）</h3>
      {champions.length === 0 ? (
        <EmptyState>当前筛选条件未命中正交簇冠军。</EmptyState>
      ) : (
        <div className="nx-grid-3">
          {champions.slice(0, 6).map((c) => (
            <div key={c.strategy_code} className="nx-gauge-box">
              <div className="nx-pick-head">
                <b>{c.display_name}</b>
                <Badge tone="good">
                  权重 {(Number(c.ensemble_weight) * 100).toFixed(1)}%（RAM保底 {(Number(c.ram_floor_weight) * 100).toFixed(1)}%）
                </Badge>
              </div>
              <div className="chips">
                <Badge tone="neutral">{LAYER_TEXT[c.layer] ?? c.layer}</Badge>
                <Badge tone="neutral">{HORIZON_BUCKET_TEXT[c.horizon_bucket] ?? c.horizon_bucket}</Badge>
                <Badge tone="neutral">μ-2φ = {c.conservative_score}</Badge>
                <Badge tone="neutral">净收益 {fmtVal(c.net_return_pct, '%')} · Sharpe {fmtVal(c.sharpe_ratio)}</Badge>
              </div>
              <div className="small muted">{c.summary}</div>
            </div>
          ))}
        </div>
      )}

      {/* Full Filtered Strategy Catalog & Real Backtest Metrics Table */}
      <h3>策略分类路由与天梯真实回测明细（当前筛选 {d.strategies.length} 套）</h3>
      {d.strategies.length === 0 ? (
        <EmptyState>当前筛选条件下没有匹配的策略。</EmptyState>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="table small">
            <thead>
              <tr>
                <th>策略名称与编码</th>
                <th>分类标签（层级 / 周期 / 流派）</th>
                <th>Glicko-2 (μ±φ) / 保守分</th>
                <th>瑞士轮战绩</th>
                <th>净收益 / 超额 / Sharpe / 回撤</th>
                <th>NW-t / CVaR / RRE / PFS</th>
                <th>权重 · 评估源 · 晋升状态</th>
              </tr>
            </thead>
            <tbody>
              {d.strategies.map((s: StrategyTaxonomyEntry) => {
                const tInfo = LIFECYCLE_TIER_TEXT[s.lifecycle_tier] ?? { label: s.lifecycle_tier, tone: 'neutral' as const };
                return (
                  <tr key={s.strategy_code}>
                    <td>
                      <b>{s.display_name}</b>
                      <div className="mono muted">{s.strategy_code}</div>
                      {s.paper_citation && <div className="muted">{s.paper_citation}</div>}
                    </td>
                    <td>
                      <div className="chips">
                        <Badge tone="neutral">{LAYER_TEXT[s.layer] ?? s.layer}</Badge>
                        <Badge tone="neutral">{HORIZON_BUCKET_TEXT[s.horizon_bucket] ?? s.horizon_bucket}</Badge>
                        <Badge tone="neutral">{PARADIGM_TEXT[s.paradigm] ?? s.paradigm}</Badge>
                      </div>
                    </td>
                    <td className="mono">
                      <div>{s.glicko_mu} ± {s.glicko_phi}</div>
                      <b>{s.conservative_score}</b>
                    </td>
                    <td className="mono">{s.swiss_wins}胜 {s.swiss_draws}平 {s.swiss_losses}负</td>
                    <td className="mono">
                      <div>净 {fmtVal(s.net_return_pct, '%')} / 超 {fmtVal(s.excess_return_pct, '%')}</div>
                      <div>SR {fmtVal(s.sharpe_ratio)} / DD {fmtVal(s.max_drawdown_pct, '%')}</div>
                    </td>
                    <td className="mono">
                      <div>t={fmtVal(s.nw_t_stat)} · CVaR={fmtVal(s.worst_cvar_pct, '%')}</div>
                      <div>{fmtVal(s.rre_stability)} / {fmtVal(s.pfs_robustness)}</div>
                    </td>
                    <td>
                      <div className="chips">
                        <Badge tone={tInfo.tone}>{tInfo.label}</Badge>
                        <Badge tone="neutral">权 {(Number(s.ensemble_weight) * 100).toFixed(1)}% (保底 {(Number(s.ram_floor_weight) * 100).toFixed(1)}%)</Badge>
                      </div>
                      <div className="mono muted">{s.evaluation_source}</div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* AlphaPROBE Discovered Candidates Pool */}
      <h3 style={{ marginTop: 16 }}>AlphaPROBE 自动挖掘与正交门控候选池（{d.discovered_candidates.length} 项）</h3>
      {d.discovered_candidates.length === 0 ? (
        <EmptyState>当前尚无自动挖掘候选策略（运行演化挖掘管线后自动展示入池与淘汰记录）。</EmptyState>
      ) : (
        <table className="table small">
          <thead>
            <tr>
              <th>候选策略</th>
              <th>父代策略 / DAG深度</th>
              <th>最大相关度 |Corr|</th>
              <th>RRE / PFS</th>
              <th>扣费净 Sharpe / NW-t</th>
              <th>门控裁决</th>
            </tr>
          </thead>
          <tbody>
            {d.discovered_candidates.map((cand) => {
              const gMeta = DISCOVERED_GATE_TEXT[cand.gate_verdict] ?? { label: cand.gate_verdict, tone: 'neutral' as const };
              return (
                <tr key={cand.candidate_id}>
                  <td>
                    <b>{cand.display_name}</b>
                    <div className="mono muted">{cand.candidate_id}</div>
                  </td>
                  <td className="mono">{cand.parent_code ?? '根节点'} · 深度 {cand.dag_depth}</td>
                  <td className="mono">{fmtVal(cand.max_corr_existing)}</td>
                  <td className="mono">{fmtVal(cand.rre_stability)} / {fmtVal(cand.pfs_robustness)}</td>
                  <td className="mono">{fmtVal(cand.net_sharpe)} / {fmtVal(cand.nw_t_stat)}</td>
                  <td><Badge tone={gMeta.tone}>{gMeta.label}</Badge></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {d.recent_pairings.length > 0 && (
        <details className="small" style={{ marginTop: 10 }}>
          <summary>查看最近一轮瑞士轮同分捉对厮杀对局记录（{d.recent_pairings.length} 场）</summary>
          <ul className="plain small" style={{ marginTop: 6 }}>
            {d.recent_pairings.map((p, idx) => (
              <li key={idx}>
                <Badge tone="neutral">第 {p.round_no} 轮</Badge>{' '}
                <span className="mono">{p.left_code}</span> 对阵 <span className="mono">{p.right_code}</span> →{' '}
                <Badge tone={p.winner === 'draw' ? 'neutral' : 'good'}>
                  {p.winner === 'left' ? '左方胜' : p.winner === 'right' ? '右方胜' : p.winner === 'draw' ? '战平' : p.winner}
                </Badge>{' '}
                <span className="mono">({p.score_delta})</span>{' '}
                <span className="muted">（{p.reason}）</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export function PapersAndBenchmarkView() {
  const arenaQ = useQuery({
    queryKey: ['bf-nexus', 'arena-papers'],
    queryFn: ({ signal }) => nexusOps.arena({}, signal),
    staleTime: 60_000,
  });
  const intQ = useQuery({
    queryKey: ['bf-nexus', 'integrity'],
    queryFn: ({ signal }) => nexusOps.integrity(signal),
    staleTime: 60_000,
  });

  if (arenaQ.isLoading || intQ.isLoading) return <Loading label="读取 2024–2026 顶会论文算子与统一国际记账对账报告…" />;
  if (arenaQ.error) return <ErrorState error={arenaQ.error} what="顶会论文算子" onRetry={() => void arenaQ.refetch()} />;
  if (intQ.error) return <ErrorState error={intQ.error} what="统一记账对账" onRetry={() => void intQ.refetch()} />;
  if (!arenaQ.data || !intQ.data) return null;

  const papers = arenaQ.data.data.top_papers_2026;
  const audit = intQ.data.data;
  const std = audit.benchmark_standard;
  const dbh = audit.database_health;

  return (
    <section aria-label="2026顶会论文算子与国际公认统一记账标准">
      <SectionTitle
        title="2024–2026 金融与计算机顶会论文实装算子矩阵"
        hint="集成 ICLR / ICML / NeurIPS / KDD / AAAI / IJCAI / Management Science / JFQA 权威方法，全部转化为零前视确定性算子。"
      />
      <div className="nx-grid-2">
        {papers.map((p) => (
          <article key={p.paper_id} className="card item">
            <div className="item-head">
              <b>{p.title}</b>
              <Badge tone="good">{p.venue_year}</Badge>
            </div>
            <div className="small muted">作者：{p.authors} · 绑定策略：<span className="mono">{p.integrated_strategy_code}</span></div>
            <code className="nx-formula mono">{p.core_formula}</code>
            <div className="small"><b>A股落地算子：</b>{p.ashare_operator}</div>
          </article>
        ))}
      </div>

      <h3 style={{ marginTop: 18 }}>
        {std.standard_id} 国际公认统一成交记账基准与 7 入口动态对账{' '}
        <Badge tone={audit.overall_status === 'healthy' ? 'good' : audit.overall_status === 'warnings_present' ? 'warn' : 'bad'}>
          {audit.overall_status} · {audit.audit_version}
        </Badge>
      </h3>
      <div className="card">
        <dl className="nx-price-strip">
          <div><dt>基准协议版本</dt><dd className="mono">{std.standard_id}</dd></div>
          <div><dt>信号与成交时序</dt><dd>{std.execution_timing}</dd></div>
          <div><dt>卖出印花税</dt><dd>{std.stamp_duty_bps} bps</dd></div>
          <div><dt>双边券商佣金</dt><dd>{std.commission_bps} bps (最低{std.min_commission_cny}元)</dd></div>
          <div><dt>过户费</dt><dd>{std.transfer_fee_bps} bps</dd></div>
          <div><dt>滑点模型</dt><dd>{std.slippage_model}</dd></div>
        </dl>
        <div className="small muted" style={{ marginTop: 6 }}>
          <b>涨跌停与排队规则：</b>{std.limit_queue_rule}
        </div>
        <div className="small muted">
          <b>统一验证协议：</b>{std.validation_protocol}
        </div>

        <table className="table small" style={{ marginTop: 10 }}>
          <thead>
            <tr>
              <th>回测/记账入口</th>
              <th>模块路径</th>
              <th>统一基准</th>
              <th>T+1约束</th>
              <th>费率与涨跌停口径</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {audit.entrypoint_accounting_parity.map((ep) => (
              <tr key={ep.entrypoint}>
                <td><b>{ep.entrypoint}</b></td>
                <td className="mono">{ep.module_path}</td>
                <td>
                  <Badge tone={ep.uses_unified_standard ? 'good' : 'neutral'}>
                    {ep.uses_unified_standard ? '统一基准对齐' : '预注册冻结影子'}
                  </Badge>
                </td>
                <td><Badge tone={ep.t1_enforced ? 'good' : 'bad'}>{ep.t1_enforced ? '严格T+1' : '未强制'}</Badge></td>
                <td className="small">
                  <div>{ep.fee_basis}</div>
                  <div className="muted">{ep.limit_rule}</div>
                </td>
                <td>{ep.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Outbox Channels & Database Health */}
      <div className="nx-grid-2" style={{ marginTop: 12 }}>
        <div className="card">
          <h4>Outbox 事件发布通道自检（{audit.outbox_event_channels.length} 个通道）</h4>
          <table className="table small">
            <thead><tr><th>事件类型</th><th>生产者接线</th><th>累计发送</th><th>最近发送时间</th></tr></thead>
            <tbody>
              {audit.outbox_event_channels.map((ch) => (
                <tr key={ch.event_type}>
                  <td className="mono">{ch.event_type}</td>
                  <td><Badge tone={ch.producer_wired ? 'good' : 'bad'}>{ch.producer_wired ? '已接线' : '未接线'}</Badge></td>
                  <td className="mono">{ch.total_emitted}</td>
                  <td className="small muted">{ch.last_emitted_at ? fmtTime(ch.last_emitted_at) : '暂无'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h4>PostgreSQL Nexus 持久化表与索引健康状态</h4>
          <dl className="nx-price-strip">
            <div><dt>Nexus 表就绪</dt><dd>{dbh.nexus_tables_ready ? '就绪' : '异常'}</dd></div>
            <div><dt>核心索引就绪</dt><dd>{dbh.performance_indexes_ready ? '就绪' : '未齐'}</dd></div>
            <div><dt>个股工作台缓存</dt><dd>{dbh.workbench_cache_rows} 行</dd></div>
            <div><dt>交叉核验消息簇</dt><dd>{dbh.cross_verified_clusters} 条</dd></div>
            <div><dt>RAPTOR 实时调权</dt><dd>{dbh.realtime_adjustments_rows} 条</dd></div>
            <div><dt>天梯评分 / 挖掘候选</dt><dd>{dbh.arena_ratings_rows} / {dbh.discovered_candidates_rows}</dd></div>
          </dl>
        </div>
      </div>

      <details className="small" style={{ marginTop: 10 }}>
        <summary>查看跨数据源字段统一命名与单位/缺失值纠正规范表（{audit.source_normalization_matrix.length} 个核心源）</summary>
        <table className="table small" style={{ marginTop: 6 }}>
          <thead><tr><th>数据源 ID</th><th>领域</th><th>原始异构字段</th><th>统一标准字段</th><th>缺失与单位防错策略</th></tr></thead>
          <tbody>
            {audit.source_normalization_matrix.map((m) => (
              <tr key={m.source_id}>
                <td className="mono">{m.source_id}</td>
                <td>{m.domain}</td>
                <td className="mono">{m.raw_fields}</td>
                <td className="mono">{m.canonical_fields}</td>
                <td>{m.missing_value_policy}；{m.unit_guard}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </section>
  );
}
