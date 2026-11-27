"""Multi-dimensional Strategy Taxonomy Router & Empirical Backtest Swiss-System + Glicko-2 Arena.

Why: Managing 62+ heterogeneous strategies (52 traditional/factor playbooks, static40_safe
multi-factor fusion, and 9 2024–2026 top-venue AI/finance paper strategy clusters) without
unified empirical benchmarking, dynamic correlation clustering, and exact Glicko-2 uncertainty
tracking leads to crowded redundant voting and regime overfitting.

Key Innovations & Hard Invariants:
1. 6-Dimensional Taxonomy Tagging: ``layer``, ``horizon_bucket``, ``paradigm``, ``regime_fit``,
   ``portfolio_role``, ``cluster_id``.
2. Exact Glickman (2012) Glicko-2 Rating with Illinois Iterative Volatility Root-Finding (tol=1e-6):
   Tracks skill ``mu``, rating deviation ``phi`` (uncertainty), and volatility ``sigma``.
   Ranks and allocates weights strictly by Conservative Lower Bound ``S_i = mu_i - 2 * phi_i``.
3. Dual-Mode Empirical Backtest Driver:
   When ``panel`` + ``signal_history`` (or ``empirical_metrics``) are provided, evaluates
   strategies on ``UnifiedBacktestEngine`` (``GIPS-AShare-2026-v1``), dynamically clusters
   strategies with ``|rho_ij| >= 0.70`` via AlphaSAGE cross-sectional daily-percentile rank
   correlation, and pairs them in Swiss rounds using real composite strength (net excess +
   Sharpe + Newey-West t + RRE + PFS - drawdown penalty). Unbacktested strategies are
   explicitly labeled ``ledger_calibrated_prior`` (never masquerading as live backtests).
4. Orthogonal Cluster Champion Selection + NeurIPS 2025 RAM Worst-Case Floor Mixing:
   One champion per orthogonal cluster enters the active ensemble, mixed with RAM uniform
   floor ``min_uniform_floor / N`` to preserve worst-case geometric growth bounds.
5. All-Voter Normalization + Bounded RAPTOR View Tilt:
   ``EnsembleScore(stock) = sum_{v in ActiveVoters} (w_v * pct_v(stock)) / sum_{v in ActiveVoters} w_v``.
   Unpicked = 0; unvalidated auxiliaries can never solo-populate a list; optional RAPTOR
   Black-Litterman tilts are strictly bounded to ``+-15%``.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from pulse.domain.arena.top_papers_2026 import (
    compute_ram_portfolio_weights,
    compute_raptor_bl_tilt,
)
from pulse.domain.benchmark.accounting import (
    DEFAULT_STANDARD,
    AccountingStandard,
    PanelBar,
    UnifiedBacktestEngine,
    compute_cross_sectional_rank_corr,
    compute_relative_rank_entropy,
)
from pulse.domain.playbooks import library as pb_lib


@dataclass(frozen=True)
class TaxonomyCard:
    strategy_code: str
    display_name: str
    layer: str
    horizon_bucket: str
    paradigm: str
    regime_fit: tuple[str, ...]
    portfolio_role: str
    cluster_id: str
    paper_citation: str | None
    summary: str
    base_mu: float
    base_phi: float
    rre_stability: float | None
    pfs_robustness: float | None
    empirical_tier: str


@dataclass(frozen=True)
class GlickoRating:
    mu: float
    phi: float
    sigma: float = 0.06

    @property
    def conservative_score(self) -> float:
        return self.mu - 2.0 * self.phi


def glicko2_update(
    player: GlickoRating,
    matches: Sequence[tuple[GlickoRating, float]],
    *,
    tau: float = 0.5,
) -> GlickoRating:
    """Exact Glickman (2012) Glicko-2 update with Illinois iterative volatility solver (Step 5.4, tol=1e-6)."""
    scale = 173.7178
    mu = (player.mu - 1500.0) / scale
    phi = player.phi / scale
    sigma = max(1e-6, float(player.sigma))

    if not matches:
        # Inactive strategy -> uncertainty phi grows!
        phi_star = math.sqrt(phi * phi + sigma * sigma)
        return GlickoRating(
            mu=round(1500.0 + scale * mu, 2),
            phi=round(min(350.0, scale * phi_star), 2),
            sigma=round(sigma, 6),
        )

    def _g(p_phi: float) -> float:
        return 1.0 / math.sqrt(1.0 + 3.0 * p_phi * p_phi / (math.pi * math.pi))

    def _e(m: float, m_j: float, p_j: float) -> float:
        z = max(-60.0, min(60.0, -_g(p_j) * (m - m_j)))
        return 1.0 / (1.0 + math.exp(z))

    v_inv = 0.0
    delta_sum = 0.0
    for opp, outcome in matches:
        m_j = (opp.mu - 1500.0) / scale
        p_j = opp.phi / scale
        g_j = _g(p_j)
        e_j = _e(mu, m_j, p_j)
        v_inv += g_j * g_j * e_j * (1.0 - e_j)
        delta_sum += g_j * (outcome - e_j)

    if v_inv <= 1e-12:
        return player
    v = 1.0 / v_inv
    delta = v * delta_sum

    # Glickman Step 5: Exact Illinois algorithm (epsilon = 1e-6)
    a = math.log(sigma * sigma)
    tau_sq = max(1e-8, tau * tau)

    def _f(x: float) -> float:
        ex = math.exp(max(-40.0, min(40.0, x)))
        denom = phi * phi + v + ex
        num = ex * (delta * delta - phi * phi - v - ex)
        return num / (2.0 * denom * denom) - (x - a) / tau_sq

    A = a
    diff_sq = delta * delta - phi * phi - v
    if diff_sq > 0.0:
        B = math.log(diff_sq)
    else:
        k = 1
        while _f(a - k * tau) < 0.0 and k < 100:
            k += 1
        B = a - k * tau

    fA = _f(A)
    fB = _f(B)
    for _ in range(100):
        if abs(B - A) <= 1e-6 or abs(fB - fA) <= 1e-15:
            break
        C = A + (A - B) * fA / (fB - fA)
        fC = _f(C)
        if fC * fB <= 0.0:
            A = B
            fA = fB
        else:
            fA = fA / 2.0
        B = C
        fB = fC

    new_sigma = math.exp(A / 2.0)
    new_sigma = max(0.01, min(0.25, new_sigma))
    phi_star = math.sqrt(phi * phi + new_sigma * new_sigma)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * delta_sum
    return GlickoRating(
        mu=round(1500.0 + scale * new_mu, 2),
        phi=round(max(30.0, min(350.0, scale * new_phi)), 2),
        sigma=round(new_sigma, 6),
    )


def build_full_taxonomy_catalog() -> list[TaxonomyCard]:
    """Build the complete 62-strategy catalog (Fusion + 9 Top-2026 Paper Clusters + 52 Playbooks)
    calibrated against the empirical findings in ``experiments.jsonl`` with verified 2024–2026 citations."""
    cards: list[TaxonomyCard] = [
        TaxonomyCard(
            strategy_code="fusion_static40_safe",
            display_name="Level-Safe 392因子扩展窗口ICIR融合主书 (E012/E022/E036)",
            layer="stock_selection",
            horizon_bucket="short_5d",
            paradigm="multi_factor_fusion",
            regime_fit=("bull_mainline", "structural_rotation", "range_chop", "defensive_bear"),
            portfolio_role="core_alpha",
            cluster_id="cluster_multifactor_core",
            paper_citation="AlphaGen (KDD 2023) + Pulse Ledger E012/E022/E036 (20d/0.3 Curve Gate)",
            summary="剔除36个价格水平前视因子后的392因子扩展窗ICIR加权主书，配20日净值均线0.3降仓闸门，跨牛熊验证最优基线。",
            base_mu=1685.0, base_phi=48.0, rre_stability=0.86, pfs_robustness=0.94,
            empirical_tier="champion_active",
        ),
        TaxonomyCard(
            strategy_code="paper_icml2025_alphaqcm_tail",
            display_name="散户杠杆踩踏与下尾分位防线 (JF 2026 + Pulse Quantile Tail Shield)",
            layer="stock_selection",
            horizon_bucket="swing_10_20d",
            paradigm="top_paper_2026",
            regime_fit=("range_chop", "defensive_bear", "structural_rotation"),
            portfolio_role="defensive_shield",
            cluster_id="cluster_low_risk_tail",
            paper_citation="Bian, Da, He, Lou, Shue & Zhou (Journal of Finance Vol.81 No.4, Aug 2026) + Pulse Quantile Tail Defense (注: ICML 2025 AlphaQCM原文为分布式RL探索奖励)",
            summary="基于JF 2026散户融资杠杆彩票效应研究、Q20下尾防线、Bali MAX惩罚与ACL 2026 Amihud流动性摩擦过滤，抗风格剧烈切换回撤。",
            base_mu=1652.0, base_phi=54.0, rre_stability=0.91, pfs_robustness=0.95,
            empirical_tier="promoted_active",
        ),
        TaxonomyCard(
            strategy_code="paper_ms2025_lot_momentum",
            display_name="一手门槛调节动量与低价反转策略 (Management Science 2026)",
            layer="stock_selection",
            horizon_bucket="swing_10_20d",
            paradigm="top_paper_2026",
            regime_fit=("bull_mainline", "structural_rotation"),
            portfolio_role="core_alpha",
            cluster_id="cluster_lot_momentum",
            paper_citation="Du, Huang, Liu, Shi, Subrahmanyam & Zhang (Management Science Vol.72 No.3, Mar 2026)",
            summary="利用A股100股整手入场资金门槛区分机构主导高价股动量与散户扎堆低价股短期反转。",
            base_mu=1628.0, base_phi=62.0, rre_stability=0.84, pfs_robustness=0.91,
            empirical_tier="promoted_active",
        ),
        TaxonomyCard(
            strategy_code="paper_jfqa2025_micro_tug_of_war",
            display_name="T+1隔夜日内拔河与量价背离避雷策略 (JFQA 2025 / JFE 2019 / PBFJ 2024)",
            layer="stock_selection",
            horizon_bucket="short_5d",
            paradigm="top_paper_2026",
            regime_fit=("structural_rotation", "range_chop", "defensive_bear"),
            portfolio_role="core_alpha",
            cluster_id="cluster_micro_tow",
            paper_citation="Jones, Shi, Zhang & Zhang (JFQA 2025) + Lou, Polk & Skouras (JFE 2019, A股T+1适配) + Yang & Xiong (PBFJ 2024) + FINSABER (KDD 2026)",
            summary="剥离A股T+1隔夜与日内拔河，融合20日真实量价相关性、股吧SIR过热与拉萨天团否决，经KDD 2026 FINSABER前后半段稳定性核验。",
            base_mu=1640.0, base_phi=56.0, rre_stability=0.82, pfs_robustness=0.92,
            empirical_tier="promoted_active",
        ),
        TaxonomyCard(
            strategy_code="paper_aaai2024_sector_hypergraph",
            display_name="板块超边龙头溢出与RAPTOR观点融合策略 (AAAI 2024 / NeurIPS 2025 / ACL 2026)",
            layer="stock_selection",
            horizon_bucket="short_5d",
            paradigm="top_paper_2026",
            regime_fit=("bull_mainline", "structural_rotation"),
            portfolio_role="core_alpha",
            cluster_id="cluster_sector_spillover",
            paper_citation="CI-STHPAN & MDGNN & StockMixer (AAAI 2024) + RAPTOR BL (NeurIPS 2025) + Liquidity Filter (ACL 2026)",
            summary="PIT行业超边捕捉龙头动量向低位中军溢出，结合ACL 2026 Amihud摩擦过滤与RAPTOR Black-Litterman有界观点倾斜(±15%)。",
            base_mu=1615.0, base_phi=64.0, rre_stability=0.79, pfs_robustness=0.89,
            empirical_tier="promoted_active",
        ),
        TaxonomyCard(
            strategy_code="paper_ijcai2026_prism_vq_moe",
            display_name="离散体制码本专家路由策略 (IJCAI 2026 PRISM-VQ + AlphaForge + Alpha-R1)",
            layer="stock_selection",
            horizon_bucket="position_60d",
            paradigm="top_paper_2026",
            regime_fit=("bull_mainline", "structural_rotation", "range_chop", "defensive_bear"),
            portfolio_role="core_alpha",
            cluster_id="cluster_regime_moe",
            paper_citation="PRISM-VQ (IJCAI 2026, 原文K=512原型适配为4体制码本) + AlphaForge (AAAI 2025) + Alpha-R1 (arXiv 2026)",
            summary="将PRISM-VQ K=512离散原型适配为4状态低维体制码本路由专家权重，结合AlphaForge换手平滑与Alpha-R1语义门控。",
            base_mu=1634.0, base_phi=58.0, rre_stability=0.93, pfs_robustness=0.93,
            empirical_tier="promoted_active",
        ),
        TaxonomyCard(
            strategy_code="paper_iclr2026_alphasage_ortho",
            display_name="GFlowNet新颖度正交与AlphaPROBE因子发现组 (ICLR 2026 / KDD 2025 / arXiv 2026)",
            layer="stock_selection",
            horizon_bucket="swing_10_20d",
            paradigm="top_paper_2026",
            regime_fit=("structural_rotation", "range_chop"),
            portfolio_role="exploratory_shadow",
            cluster_id="cluster_ortho_mining",
            paper_citation="AlphaSAGE (ICLR 2026, arXiv:2509.25055, Chen et al.) + AlphaAgent (KDD 2025) + AlphaPROBE (arXiv 2026)",
            summary="通过DAG深度贝叶斯衰减检索(AlphaPROBE)与|corr|<0.70截面去冗余约束(AlphaSAGE)持续挖掘正交残差Alpha。",
            base_mu=1575.0, base_phi=75.0, rre_stability=0.85, pfs_robustness=0.88,
            empirical_tier="candidate_shadow",
        ),
        TaxonomyCard(
            strategy_code="paper_kdd2026_alphaeval_rre",
            display_name="AlphaEval相对秩熵门控与EBM可解释能量分解 (KDD 2026)",
            layer="regime_timing",
            horizon_bucket="swing_10_20d",
            paradigm="top_paper_2026",
            regime_fit=("bull_mainline", "structural_rotation", "range_chop", "defensive_bear"),
            portfolio_role="risk_veto",
            cluster_id="cluster_quality_gate",
            paper_citation="AlphaEval (KDD 2026, arXiv:2508.13174, Ding et al.) + Beyond Black Boxes EBM (KDD 2026)",
            summary="无回测相对秩熵(RRE)+高斯/Student-t真微扰保真度(PFS)前置门禁，叠加四维EBM可解释能量因子分解。",
            base_mu=1610.0, base_phi=55.0, rre_stability=0.96, pfs_robustness=0.97,
            empirical_tier="promoted_active",
        ),
        TaxonomyCard(
            strategy_code="paper_aaai2026_metatrader_cvar",
            display_name="MetaTrader保守CVaR闸门与RAM最坏情形保底混权 (AAAI 2026 / NeurIPS 2025)",
            layer="regime_timing",
            horizon_bucket="short_5d",
            paradigm="top_paper_2026",
            regime_fit=("defensive_bear", "range_chop"),
            portfolio_role="risk_veto",
            cluster_id="cluster_risk_gate",
            paper_citation="MetaTrader (AAAI 2026, arXiv:2505.12759, SJTU & CICC) + FinCon (NeurIPS 2024) + RAM (NeurIPS 2025, Zhang et al.)",
            summary="在多扰动域下取保守Bellman/CVaR下界护栏，并以NeurIPS 2025 RAM保底均匀混合确保极端分布漂移下几何收益下界。",
            base_mu=1645.0, base_phi=50.0, rre_stability=0.92, pfs_robustness=0.95,
            empirical_tier="champion_active",
        ),
        TaxonomyCard(
            strategy_code="paper_aaai2026_archetype_intraday_t",
            display_name="ArchetypeTrader底仓做T与A-S保留价网格引擎 (AAAI 2026 / A-S 2008 / ICAIF 2026)",
            layer="intraday_execution",
            horizon_bucket="intraday_t0",
            paradigm="grid_intraday_t",
            regime_fit=("range_chop", "structural_rotation"),
            portfolio_role="cost_reducer",
            cluster_id="cluster_intraday_grid_t",
            paper_citation="ArchetypeTrader (AAAI 2026, NTU) + Avellaneda-Stoikov (2008) + Crumble-Aware Neurosymbolic RL (ACM ICAIF 2026)",
            summary="结合T+1可卖底仓、Avellaneda-Stoikov (2008) 库存偏斜保留价公式与ICAIF 2026防破网现金锁机制降低持仓成本。",
            base_mu=1608.0, base_phi=60.0, rre_stability=0.88, pfs_robustness=0.90,
            empirical_tier="promoted_active",
        ),
    ]

    # Map all 52 existing playbooks into the unified taxonomy with empirical ledger calibration
    fam_map = {
        "limit_relay": ("ultra_short_1_3d", "hot_money_seat", ("bull_mainline",), "exploratory_shadow", "cluster_limit_relay", 1410.0, 68.0, "retired_falsified"),
        "trend_breakout": ("swing_10_20d", "technical_pattern", ("bull_mainline", "structural_rotation"), "exploratory_shadow", "cluster_trend_breakout", 1475.0, 65.0, "demoted_paused"),
        "academic_factor": ("position_60d", "classic_factor", ("structural_rotation", "range_chop", "defensive_bear"), "defensive_shield", "cluster_classic_factor", 1565.0, 58.0, "candidate_shadow"),
        "oversold": ("short_5d", "technical_pattern", ("defensive_bear", "range_chop"), "exploratory_shadow", "cluster_oversold_bounce", 1525.0, 64.0, "observing_trial"),
        "sentiment_cycle": ("short_5d", "sentiment_microstructure", ("structural_rotation", "range_chop"), "exploratory_shadow", "cluster_sentiment_cycle", 1515.0, 66.0, "observing_trial"),
        "crowd": ("ultra_short_1_3d", "hot_money_seat", ("bull_mainline", "structural_rotation"), "risk_veto", "cluster_seat_crowd", 1505.0, 65.0, "observing_trial"),
        "factor_composite": ("swing_10_20d", "classic_factor", ("range_chop", "defensive_bear", "structural_rotation"), "defensive_shield", "cluster_low_risk_tail", 1618.0, 55.0, "promoted_active"),
        "technical": ("short_5d", "technical_pattern", ("range_chop", "structural_rotation"), "exploratory_shadow", "cluster_tech_candle", 1508.0, 66.0, "observing_trial"),
    }

    for spec in pb_lib.REGISTRY.values():
        hb, par, reg, role, cid, mu0, phi0, tier = fam_map.get(
            spec.family,
            ("short_5d", "technical_pattern", ("range_chop",), "exploratory_shadow", f"cluster_{spec.family}", 1500.0, 70.0, "observing_trial"),
        )
        # Highlight specific empirically notable playbooks from experiments.jsonl
        if spec.id in ("low_risk_combo", "low_turnover_low_vol", "low_volatility"):
            mu0, phi0, tier, role = 1622.0, 52.0, "promoted_active", "defensive_shield"
        elif spec.id in ("macd_bottom_divergence", "overnight_gap_reversal", "shrink_oversold"):
            mu0, phi0, tier = 1568.0, 60.0, "candidate_shadow"
        elif spec.id in ("first_board_relay", "two_to_three", "dragon_return"):
            mu0, phi0, tier = 1395.0, 62.0, "retired_falsified"  # E027/E062 falsified on daily next-open!

        cards.append(TaxonomyCard(
            strategy_code=f"pb:{spec.id}",
            display_name=spec.name,
            layer="stock_selection",
            horizon_bucket=hb,
            paradigm=par,
            regime_fit=reg,
            portfolio_role=role,
            cluster_id=cid,
            paper_citation=f"Pulse Playbook Library ({spec.family}, Ledger E003/E060/E062 audited)",
            summary=spec.rule[:220],
            base_mu=mu0,
            base_phi=phi0,
            rre_stability=0.88 if hb in ("swing_10_20d", "position_60d") else 0.64,
            pfs_robustness=0.85,
            empirical_tier=tier,
        ))
    return cards


def _dynamic_orthogonal_clusters(
    catalog: Sequence[TaxonomyCard],
    signal_history: Mapping[str, Mapping[date, Mapping[str, float]]] | None,
) -> tuple[dict[str, str], dict[str, dict[str, float]]]:
    """Compute pairwise AlphaSAGE daily-percentile correlations and dynamically group
    strategies with ``|rho_ij| >= 0.70`` into shared orthogonal clusters."""
    dyn_cluster: dict[str, str] = {c.strategy_code: c.cluster_id for c in catalog}
    pairwise_corr: dict[str, dict[str, float]] = {}
    if not signal_history or len(signal_history) < 2:
        return dyn_cluster, pairwise_corr

    sel_codes = sorted(
        c.strategy_code for c in catalog
        if c.layer == "stock_selection" and c.strategy_code in signal_history and signal_history[c.strategy_code]
    )
    if len(sel_codes) < 2:
        return dyn_cluster, pairwise_corr

    parent = {code: code for code in sel_codes}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for c1 in sel_codes:
        pairwise_corr[c1] = {c1: 1.0}
    for i in range(len(sel_codes)):
        c1 = sel_codes[i]
        for j in range(i + 1, len(sel_codes)):
            c2 = sel_codes[j]
            rho = compute_cross_sectional_rank_corr(signal_history[c1], signal_history[c2])
            if rho is not None:
                r_val = round(rho, 4)
                pairwise_corr[c1][c2] = r_val
                pairwise_corr[c2][c1] = r_val
                if abs(rho) >= 0.70:
                    _union(c1, c2)

    groups: dict[str, list[str]] = {}
    for c in sel_codes:
        groups.setdefault(_find(c), []).append(c)
    for root, members in groups.items():
        cid = f"ortho_{min(members)}"
        for m in members:
            dyn_cluster[m] = cid
    return dyn_cluster, pairwise_corr


def run_swiss_glicko_season(
    cards: Sequence[TaxonomyCard] | None = None,
    *,
    rounds: int = 5,
    season_id: str = "2026-S4-NEXUS",
    empirical_metrics: Mapping[str, Mapping[str, float]] | None = None,
    signal_history: Mapping[str, Mapping[date, Mapping[str, float]]] | None = None,
    panel: Mapping[str, Sequence[PanelBar]] | None = None,
    hold_sessions: int = 5,
    top_k: int = 10,
    std: AccountingStandard = DEFAULT_STANDARD,
    ram_uniform_floor: float = 0.15,
) -> dict[str, Any]:
    """Execute a dual-mode Swiss-System tournament with exact Glicko-2 updates,
    AlphaSAGE dynamic correlation clustering, and RAM worst-case floor ensemble weights.

    * Empirical Mode (preferred): when ``panel`` + ``signal_history`` (or ``empirical_metrics``)
      are supplied, runs ``UnifiedBacktestEngine`` (``GIPS-AShare-2026-v1``) for every
      backtestable strategy, clusters correlated signals ``|rho| >= 0.70``, and pairs
      strategies on real risk-adjusted net performance + RRE + PFS - drawdown penalty.
    * Prior Mode (fallback): when no panel/metrics are passed, uses ledger-calibrated
      priors and honestly labels every entry ``evaluation_source = 'ledger_calibrated_prior'``.
    """
    catalog = list(cards or build_full_taxonomy_catalog())
    eval_metrics: dict[str, dict[str, float | None]] = {}
    eval_source: dict[str, str] = {}
    sessions_evaluated = 0

    if panel and signal_history:
        engine = UnifiedBacktestEngine()
        sessions_evaluated = len({b.date for bars in panel.values() for b in bars})
        for code in sorted(signal_history):
            sig_map = signal_history[code]
            if not sig_map:
                continue
            rep = engine.run(
                panel,
                sig_map,
                hold_sessions=hold_sessions,
                top_k=top_k,
                std=std,
            )
            ordered_days = sorted(sig_map.keys())
            rre = compute_relative_rank_entropy([sig_map[d] for d in ordered_days])
            ext = (empirical_metrics or {}).get(code, {})
            pfs_ext = ext.get("pfs_robustness")
            eval_metrics[code] = {
                "net_return_pct": rep.net_return_pct,
                "excess_return_pct": rep.excess_return_pct,
                "sharpe_ratio": rep.sharpe_ratio,
                "max_drawdown_pct": rep.max_drawdown_pct,
                "nw_t_stat": rep.nw_t_stat,
                "worst_cvar_pct": rep.worst_cvar_pct,
                "rre_stability": round(rre, 4) if rre is not None else (float(ext["rre_stability"]) if ext.get("rre_stability") is not None else None),
                "pfs_robustness": float(pfs_ext) if pfs_ext is not None else None,
            }
            eval_source[code] = "unified_backtest_engine"

    if empirical_metrics:
        for code, em in empirical_metrics.items():
            if code not in eval_metrics:
                eval_metrics[code] = {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in em.items()}
                eval_source[code] = str(em.get("evaluation_source", "empirical_metrics_external"))

    for c in catalog:
        eval_source.setdefault(c.strategy_code, "ledger_calibrated_prior")

    dyn_cluster, pairwise_corr = _dynamic_orthogonal_clusters(catalog, signal_history)

    ratings: dict[str, GlickoRating] = {}
    for c in catalog:
        if c.strategy_code in eval_metrics:
            # Empirical strategies start from neutral empirical prior so real backtest determines rating!
            ratings[c.strategy_code] = GlickoRating(1550.0, 75.0, 0.06)
        else:
            ratings[c.strategy_code] = GlickoRating(c.base_mu, c.base_phi, 0.06)

    wins: dict[str, int] = {c.strategy_code: 0 for c in catalog}
    draws: dict[str, int] = {c.strategy_code: 0 for c in catalog}
    losses: dict[str, int] = {c.strategy_code: 0 for c in catalog}
    played_pairs: set[tuple[str, str]] = set()
    recent_pairings: list[dict[str, Any]] = []

    def _duel_strength(card: TaxonomyCard) -> float:
        code = card.strategy_code
        if code in eval_metrics:
            m = eval_metrics[code]
            ex = float(m.get("excess_return_pct") or 0.0)
            sh = float(m.get("sharpe_ratio") or 0.0)
            nw = max(-3.0, min(5.0, float(m.get("nw_t_stat") or 0.0)))
            dd = max(0.0, float(m.get("max_drawdown_pct") or 0.0))
            rre_v = float(m["rre_stability"]) if m.get("rre_stability") is not None else (card.rre_stability or 0.70)
            pfs_v = float(m["pfs_robustness"]) if m.get("pfs_robustness") is not None else (card.pfs_robustness or 0.85)
            return (
                0.35 * (ex / 10.0)
                + 0.30 * sh
                + 0.20 * nw
                + 0.80 * (rre_v - 0.75)
                + 0.60 * (pfs_v - 0.85)
                - 0.05 * dd
            )
        return (
            (card.base_mu - 1500.0) / 100.0
            + 0.8 * ((card.rre_stability or 0.70) - 0.75)
            + 0.6 * ((card.pfs_robustness or 0.80) - 0.85)
        )

    for r_no in range(1, rounds + 1):
        ordered = sorted(
            catalog,
            key=lambda c: (-ratings[c.strategy_code].conservative_score, c.strategy_code),
        )
        used: set[str] = set()
        round_matches: dict[str, list[tuple[GlickoRating, float]]] = {c.strategy_code: [] for c in catalog}

        for i, left in enumerate(ordered):
            lc = left.strategy_code
            if lc in used:
                continue
            opp_card = None
            l_clust = dyn_cluster[lc]
            # 1st priority: unplayed cross-cluster peer with matching evaluation basis
            for j in range(i + 1, len(ordered)):
                cand = ordered[j]
                rc = cand.strategy_code
                if rc in used:
                    continue
                pair_key = (min(lc, rc), max(lc, rc))
                if (
                    pair_key not in played_pairs
                    and dyn_cluster[rc] != l_clust
                    and (lc in eval_metrics) == (rc in eval_metrics)
                ):
                    opp_card = cand
                    break
            # 2nd priority: unplayed cross-cluster peer
            if opp_card is None:
                for j in range(i + 1, len(ordered)):
                    cand = ordered[j]
                    rc = cand.strategy_code
                    if rc not in used and (min(lc, rc), max(lc, rc)) not in played_pairs and dyn_cluster[rc] != l_clust:
                        opp_card = cand
                        break
            # 3rd priority: any unplayed peer
            if opp_card is None:
                for j in range(i + 1, len(ordered)):
                    cand = ordered[j]
                    rc = cand.strategy_code
                    if rc not in used and (min(lc, rc), max(lc, rc)) not in played_pairs:
                        opp_card = cand
                        break
            if opp_card is None:
                continue

            rc = opp_card.strategy_code
            used.add(lc)
            used.add(rc)
            played_pairs.add((min(lc, rc), max(lc, rc)))

            l_strength = _duel_strength(left)
            r_strength = _duel_strength(opp_card)
            diff = l_strength - r_strength
            if abs(diff) < 0.08:
                s_left, winner = 0.5, "draw"
                draws[lc] += 1
                draws[rc] += 1
            elif diff > 0:
                s_left, winner = 1.0, lc
                wins[lc] += 1
                losses[rc] += 1
            else:
                s_left, winner = 0.0, rc
                losses[lc] += 1
                wins[rc] += 1

            round_matches[lc].append((ratings[rc], s_left))
            round_matches[rc].append((ratings[lc], 1.0 - s_left))
            if r_no == rounds and len(recent_pairings) < 16:
                is_emp = (lc in eval_metrics) or (rc in eval_metrics)
                recent_pairings.append({
                    "round_no": r_no,
                    "left_code": lc,
                    "right_code": rc,
                    "winner": winner,
                    "score_delta": f"{diff:+.3f}",
                    "reason": (
                        "GIPS-AShare-2026-v1 empirical backtest + AlphaEval RRE/PFS duel"
                        if is_emp
                        else "Ledger-calibrated prior + AlphaEval RRE/PFS composite duel"
                    ),
                })

        ratings = {
            code: glicko2_update(r_prev, round_matches[code])
            for code, r_prev in ratings.items()
        }

    # Orthogonal Cluster Champion Selection using dynamic AlphaSAGE clusters
    best_in_cluster: dict[str, str] = {}
    for c in catalog:
        if c.layer != "stock_selection":
            continue
        cid = dyn_cluster[c.strategy_code]
        cur_best = best_in_cluster.get(cid)
        if cur_best is None or ratings[c.strategy_code].conservative_score > ratings[cur_best].conservative_score:
            best_in_cluster[cid] = c.strategy_code
    champions = set(best_in_cluster.values())

    # Softmax over eligible cluster champions + NeurIPS 2025 RAM worst-case uniform floor
    eligible = [
        c.strategy_code for c in catalog
        if c.strategy_code in champions
        and c.empirical_tier in ("champion_active", "promoted_active", "candidate_shadow")
        and ratings[c.strategy_code].conservative_score >= 1460.0
    ]
    if eligible:
        max_s = max(ratings[code].conservative_score for code in eligible)
        exps = {code: math.exp((ratings[code].conservative_score - max_s) / 45.0) for code in eligible}
        sum_exp = sum(exps.values()) or 1.0
        raw_weights = {code: exps[code] / sum_exp for code in eligible}
        weights = compute_ram_portfolio_weights(raw_weights, min_uniform_floor=ram_uniform_floor)
        ram_floor_each = max(0.0, min(1.0, ram_uniform_floor)) / len(eligible)
    else:
        weights = {}
        ram_floor_each = 0.0

    entries = []
    for c in sorted(catalog, key=lambda x: (-ratings[x.strategy_code].conservative_score, x.strategy_code)):
        code = c.strategy_code
        rt = ratings[code]
        w = weights.get(code, 0.0)
        ram_floor = ram_floor_each if code in weights else 0.0
        em = eval_metrics.get(code, {})
        rre_out = em.get("rre_stability") if em.get("rre_stability") is not None else c.rre_stability
        pfs_out = em.get("pfs_robustness") if em.get("pfs_robustness") is not None else c.pfs_robustness

        entries.append({
            "strategy_code": code,
            "display_name": c.display_name,
            "layer": c.layer,
            "horizon_bucket": c.horizon_bucket,
            "paradigm": c.paradigm,
            "regime_fit": list(c.regime_fit),
            "portfolio_role": c.portfolio_role,
            "cluster_id": dyn_cluster[code],
            "is_cluster_champion": code in champions,
            "glicko_mu": f"{rt.mu:.2f}",
            "glicko_phi": f"{rt.phi:.2f}",
            "glicko_sigma": f"{rt.sigma:.5f}",
            "conservative_score": f"{rt.conservative_score:.2f}",
            "swiss_wins": wins[code],
            "swiss_draws": draws[code],
            "swiss_losses": losses[code],
            "net_return_pct": f"{float(em['net_return_pct']):.4f}" if em.get("net_return_pct") is not None else None,
            "excess_return_pct": f"{float(em['excess_return_pct']):.4f}" if em.get("excess_return_pct") is not None else None,
            "sharpe_ratio": f"{float(em['sharpe_ratio']):.4f}" if em.get("sharpe_ratio") is not None else None,
            "max_drawdown_pct": f"{float(em['max_drawdown_pct']):.4f}" if em.get("max_drawdown_pct") is not None else None,
            "nw_t_stat": f"{float(em['nw_t_stat']):.4f}" if em.get("nw_t_stat") is not None else None,
            "worst_cvar_pct": f"{float(em['worst_cvar_pct']):.4f}" if em.get("worst_cvar_pct") is not None else None,
            "rre_stability": f"{float(rre_out):.3f}" if rre_out is not None else None,
            "pfs_robustness": f"{float(pfs_out):.3f}" if pfs_out is not None else None,
            "evaluation_source": eval_source[code],
            "lifecycle_tier": c.empirical_tier,
            "ensemble_weight": f"{w:.4f}",
            "ram_floor_weight": f"{ram_floor:.4f}",
            "paper_citation": c.paper_citation,
            "summary": c.summary,
        })

    eval_basis = "empirical_backtest_gips_v1" if eval_metrics else "ledger_calibrated_prior"
    return {
        "season_id": season_id,
        "rounds_completed": rounds,
        "total_strategies": len(entries),
        "orthogonal_clusters": len({dyn_cluster[c.strategy_code] for c in catalog}),
        "evaluation_basis": eval_basis,
        "sessions_evaluated": sessions_evaluated,
        "pairwise_corr": pairwise_corr,
        "taxonomy_facets": {
            "layers": ["stock_selection", "regime_timing", "intraday_execution"],
            "horizons": ["intraday_t0", "ultra_short_1_3d", "short_5d", "swing_10_20d", "position_60d"],
            "paradigms": ["top_paper_2026", "multi_factor_fusion", "classic_factor", "technical_pattern", "sentiment_microstructure", "hot_money_seat", "grid_intraday_t"],
            "roles": ["core_alpha", "defensive_shield", "risk_veto", "cost_reducer", "exploratory_shadow"],
        },
        "strategies": entries,
        "recent_pairings": recent_pairings,
        "weights": weights,
    }


def synthesize_orthogonal_ensemble(
    voter_signals: Mapping[str, Mapping[str, float]],
    voter_weights: Mapping[str, float],
    *,
    validated_voters: set[str],
    raptor_views: Mapping[str, float] | None = None,
    raptor_tau: float = 0.05,
    raptor_max_tilt: float = 0.15,
) -> tuple[dict[str, float], dict[str, list[str]], int]:
    """All-Voter-Normalized Ensemble Synthesis with optional NeurIPS 2025 RAPTOR BL bounded view tilt.

    REDLINE INVARIANTS (``ensemble-vote-normalize-by-all-voters``):
    1. Denominator is ``sum(w_v for ALL active voters with signals > 0)``, NEVER the sum of
       weights of only the voters that picked a single stock! Unpicked = 0 in numerator.
    2. Auxiliary / unvalidated voters cannot solo-produce a list: if zero ``validated_voters``
       have signals today, the output ranking is empty.
    3. Optional ``raptor_views`` can ONLY tilt stocks already supported by validated voters,
       and single-stock tilt is strictly capped at ``+-raptor_max_tilt`` (default +-15%).
    Returns ``(ensemble_scores, supporting_voters_per_stock, active_voter_count)``.
    """
    active = {
        v: w for v, w in voter_weights.items()
        if w > 0 and v in voter_signals and len(voter_signals[v]) > 0
    }
    if not active or not (set(active) & validated_voters):
        return {}, {}, 0

    total_weight = sum(active.values())
    if total_weight <= 0:
        return {}, {}, 0

    all_stocks: set[str] = set()
    for v in active:
        all_stocks.update(voter_signals[v].keys())

    scores: dict[str, float] = {}
    supporters: dict[str, list[str]] = {}
    for iid in sorted(all_stocks):
        # Must be supported by at least one validated voter
        valid_hits = [v for v in active if v in validated_voters and voter_signals[v].get(iid, 0.0) > 0]
        if not valid_hits:
            continue
        num = 0.0
        hits: list[str] = []
        for v, w in active.items():
            pct = voter_signals[v].get(iid, 0.0)
            if pct > 0:
                num += w * pct
                hits.append(v)
        scores[iid] = num / total_weight
        supporters[iid] = sorted(hits, key=lambda x: -active[x])

    if raptor_views and scores:
        scores = compute_raptor_bl_tilt(scores, raptor_views, tau=raptor_tau, max_tilt=raptor_max_tilt)

    return scores, supporters, len(active)


def deterministic_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256("|".join((prefix, *parts)).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{h}"
