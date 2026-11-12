/**
 * 市场总览 (PEX01 / PEX09): sentiment thermometer, market vitals, limit ladder with promotion, seal clock (declared gap),
 * breadth histogram, theme resonance, growth-streak strip, regime banner and the live event tape.
 * Every number is an artifact of the pinned release (pulse.market_today); this page formats, it never recomputes.
 * The thermometer is a descriptive, uncalibrated gauge and says so.
 */
import { Link } from 'react-router-dom';
import { Badge } from '../../../components/Badges';
import { MetaStrip } from '../../../components/MetaStrip';
import { ReleaseBar } from '../../../components/ReleaseBar';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { useReleaseSelection } from '../../../hooks/useRelease';
import { fmtInt } from '../../../lib/format';
import './ext-a.css';
import { Bars, Gauge, Sparkline, num, pctText } from './charts';
import { STREAM_LABEL, useMarketToday, usePanel, useReleaseStream } from './hooks';
import { TapePanel } from './TapePanel';
import type { Breadth, GrowthStrip, Ladder, Regime, Themes, Thermometer, Vitals } from './types';

const bigWan = (v: string | null | undefined): string => {
  const n = num(v);
  return n == null ? '—' : n >= 1e8 ? `${(n / 1e8).toFixed(0)} 亿` : `${(n / 1e4).toFixed(0)} 万`;
};

function Mood({ t }: { t: Thermometer }) {
  const score = num(t.score);
  const edges = t.bands.map((b) => b.upper);
  return (
    <section className="card ea-mood" aria-label="情绪温度计">
      <div className="ea-head"><h3>情绪温度计</h3><Badge tone="warn" title={t.version}>未校准</Badge><span className="muted small">会话 {t.session}</span></div>
      <div className="ea-mood-main">
        <div className="ea-score mono" aria-hidden="true">{score == null ? '—' : score.toFixed(1)}</div>
        <div>
          <b className="ea-word">{t.band?.word ?? '无法计算'}</b>
          <p className="muted small">{t.band?.hint ?? `缺少组成项：${t.missing_components.join('、')}；不补 0。`}</p>
          {t.trend_word && <p className="small">近 5 日 {t.trend_word}（{pctText(t.trend_delta_5d, 1, true).replace('%', ' 分')}）</p>}
        </div>
      </div>
      <Gauge score={score} edges={edges} words={t.bands.map((b) => b.word)} />
      <Sparkline label="情绪分近 60 日" values={t.history.map((h) => num(h.score))} />
      <details className="small">
        <summary>权重与组成项（旧站声明，未经校准，仅作描述）</summary>
        <table className="table">
          <thead><tr><th>组成项</th><th>权重</th><th>当前值</th></tr></thead>
          <tbody>
            {Object.entries(t.weights).map(([k, w]) => (
              <tr key={k}><td>{{ limit_ratio: '涨停占比', breadth: '涨跌广度', height: '连板高度', seal_quality: '封板质量' }[k] ?? k}</td><td className="mono">{w}</td><td className="mono">{t.components[k] ?? '缺失'}</td></tr>
            ))}
          </tbody>
        </table>
        <p className="muted">版本 {t.version}。分值不是预测，也没有经过结果检验。</p>
      </details>
    </section>
  );
}

function VitalsTiles({ v }: { v: Vitals }) {
  const tile = (label: string, value: string, note?: string, tone?: string) => (
    <div className="card ea-tile"><div className="muted small">{label}</div><div className={`ea-tile-v mono ${tone ?? ''}`}>{value}</div>{note && <div className="small muted">{note}</div>}</div>
  );
  const none = (n: number | null) => (n == null ? '—' : fmtInt(n));
  return (
    <section aria-label="市场体征">
      <div className="ea-tiles">
        {tile('涨停', none(v.limit_up), `封板率 ${pctText(v.seal_rate_pct, 1, false)}`, 'up')}
        {tile('炸板', none(v.broken), '触及涨停价但收盘未封住', 'down')}
        {tile('跌停', none(v.limit_down))}
        {tile('最高连板', v.max_height == null ? '—' : `${v.max_height} 板`, v.max_height_stocks.map((s) => s.name).join('、'))}
        {tile('昨日涨停溢价', pctText(v.premium.close_pct), `开盘 ${pctText(v.premium.open_pct)} · 样本 ${v.premium.sample_count}`)}
        {tile('成交额（估算）', bigWan(v.volume.amount_estimate_cny), v.volume.ratio_vs_20d_mean ? `量比近 20 日均 ${v.volume.ratio_vs_20d_mean}` : '近 20 日均量不足', undefined)}
        {tile('上涨 / 下跌', `${fmtInt(v.up)} / ${fmtInt(v.down)}`, `平盘 ${fmtInt(v.flat)}`)}
      </div>
      <p className="muted small">
        统计范围：沪深主板，{fmtInt(v.coverage.observed)}/{v.coverage.expected_instruments == null ? '?' : fmtInt(v.coverage.expected_instruments)} 只有当日日线；
        {v.coverage.limit_unverifiable > 0 && `${v.coverage.limit_unverifiable} 只缺少已核实的涨跌停规则，不计入涨停统计；`}
        成交额为“股数×收盘价”估算（日线没有成交额字段）。
      </p>
    </section>
  );
}

function LadderView({ l }: { l: Ladder }) {
  return (
    <section className="card" aria-label="连板天梯">
      <div className="ea-head"><h3>连板天梯</h3><span className="muted small">会话 {l.session}</span></div>
      {l.rungs.length === 0 ? <EmptyState>当日没有涨停。</EmptyState> : (
        <table className="table ea-ladder">
          <thead><tr><th>梯队</th><th>封住</th><th>炸板</th><th>晋级率</th><th>代表股</th></tr></thead>
          <tbody>
            {l.rungs.map((r) => (
              <tr key={r.streak}>
                <td><b>{r.label}</b></td>
                <td className="mono up">{r.sealed_count}</td>
                <td className="mono down">{r.broken_count}</td>
                <td className="mono">
                  {r.promotion == null ? <span className="muted">—</span> : r.promotion.rate_pct == null ? '无可评估样本'
                    : <span title={`昨日 ${r.promotion.from_streak} 板共 ${r.promotion.frozen_count} 只，晋级 ${r.promotion.promoted}，今日无行情 ${r.promotion.missing}（不计失败）`}>{pctText(r.promotion.rate_pct, 1, false)}（{r.promotion.promoted}/{r.promotion.frozen_count - r.promotion.missing}）</span>}
                </td>
                <td className="small">{r.sealed.slice(0, 5).map((s) => s.name).join('、')}{r.broken.length > 0 && <span className="muted"> ｜炸板：{r.broken.slice(0, 3).map((s) => s.name).join('、')}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted small">{l.note}</p>
    </section>
  );
}

function ThemesView({ t, releaseId }: { t: Themes; releaseId: string | null }) {
  return (
    <section className="card" aria-label="题材共振">
      <div className="ea-head"><h3>题材共振</h3><Badge tone="warn" title={t.version}>未校准</Badge></div>
      {t.themes.length === 0 ? <EmptyState>暂无可排序的板块。</EmptyState> : (
        <ol className="ea-themes">
          {t.themes.map((x) => (
            <li key={x.node_id}>
              <Link to={`/sector-detail?node=${encodeURIComponent(x.node_id)}${releaseId ? `&release=${releaseId}` : ''}`}><b>{x.name}</b></Link>
              <span className="mono">强度 {x.strength}</span>
              <span className="small muted">涨停 {x.limit_up} · 3连阳+ {x.growth_streak_ge3} · 均涨 {pctText(x.avg_pct)} · 龙头 {x.leader.name}</span>
            </li>
          ))}
        </ol>
      )}
      <p className="muted small">强度 = {t.formula}（旧站规则，未经校准，仅作描述）。</p>
    </section>
  );
}

function GrowthView({ g }: { g: GrowthStrip }) {
  return (
    <section className="card" aria-label="连阳梯度">
      <div className="ea-head"><h3>连阳梯度</h3><span className="muted small">3 连阳及以上 {fmtInt(g.ge3_total)} 只</span></div>
      <div className="ea-growth">
        {g.buckets.map((b) => (
          <div key={b.streak} className="ea-growth-cell" title={b.top.map((s) => `${s.name} ${pctText(s.pct)}`).join('\n')}>
            <div className="mono ea-growth-n">{fmtInt(b.count)}</div><div className="small muted">{b.label}</div>
          </div>
        ))}
      </div>
      <p className="muted small">{g.definition}</p>
    </section>
  );
}

function RegimeBanner({ r }: { r: Regime }) {
  if (!r.available || !r.current) return <section className="card"><h3>市场周期</h3><EmptyState>缺少指数日线，无法判定（{r.reason}）。</EmptyState></section>;
  const c = r.current;
  return (
    <section className="card ea-regime-banner" aria-label="当前市场周期">
      <div className="ea-head"><h3>当前市场周期</h3><span className="muted small">{c.date}（指数数据截至 {r.index?.through}{r.index?.fallback ? `，使用 ${r.index.name} 代替中证全指` : ''}）</span><span className="grow" /><Link to="/regime">查看时间线 →</Link></div>
      <p className="ea-regime-label"><b>{c.label}</b></p>
      <ul className="small">{c.explanation.map((s) => <li key={s}>{s}</li>)}</ul>
    </section>
  );
}

export function MarketPage() {
  const rel = useReleaseSelection();
  const rid = rel.effectiveReleaseId;
  const stream = useReleaseStream();
  const today = useMarketToday(rid);
  const data = today.data?.data;
  const th = usePanel<Thermometer>(data, 'thermometer');
  const vi = usePanel<Vitals>(data, 'vitals');
  const la = usePanel<Ladder>(data, 'ladder');
  const br = usePanel<Breadth>(data, 'breadth');
  const tm = usePanel<Themes>(data, 'themes');
  const gr = usePanel<GrowthStrip>(data, 'growth_strip');
  const rg = usePanel<Regime>(data, 'regime');
  const stale = today.data?.meta.freshness_state === 'stale';
  const noComponent = !!data && data.state.length === 0;
  return (
    <div className="page ea-page">
      <ReleaseBar rel={rel} displayedReleaseId={today.data?.meta.release_id} />
      <SectionTitle title="市场总览" hint="沪深主板当日日线统计。数字全部来自所选发布包里已固定的产物；页面只负责展示，不重算。"
        right={<Badge tone={stream === 'open' ? 'good' : 'neutral'}>{STREAM_LABEL[stream]}</Badge>} />
      {!rid && <Loading />}
      {today.isLoading && <Loading />}
      {today.error && <ErrorState error={today.error} what="市场总览" onRetry={() => void today.refetch()} />}
      {today.data && <MetaStrip meta={today.data.meta} title="市场总览" />}
      {stale && <p className="flag flag-warn" role="alert">这是旧发布包（已被取代、已过期或数据过旧），下面的内容不是当前分析。</p>}
      {noComponent && <EmptyState>该发布包没有市场状态组件，无法显示市场总览。</EmptyState>}
      {th.body && <Mood t={th.body} />}
      {vi.body && <VitalsTiles v={vi.body} />}
      <div className="ea-two">
        {la.body && <LadderView l={la.body} />}
        <section className="card" aria-label="封板时钟">
          <div className="ea-head"><h3>封板时钟</h3><Badge tone="warn">待接入</Badge></div>
          <EmptyState>
            {data?.capability_gaps.filter((g) => g.reason_code === 'NO_INTRADAY_SEAL_TIMES').map((g) => g.requirement)[0]
              ?? '首次封板时间需要盘中数据'}。目前只有日线和少量报价快照，所以不显示封板时间分布，也不编造。
          </EmptyState>
        </section>
      </div>
      {br.body && (
        <section className="card" aria-label="涨跌分布">
          <div className="ea-head"><h3>涨跌分布</h3><span className="muted small">站上 20 日线 {pctText(br.body.above_ma20.pct, 1, false)}（{fmtInt(br.body.above_ma20.count)}/{fmtInt(br.body.above_ma20.of)}）</span></div>
          <Bars bins={br.body.bins} label="涨跌幅分布，11 档" />
          {br.body.pct_unverifiable > 0 && <p className="muted small">{br.body.pct_unverifiable} 只因前收缺失或涨跌幅超出板块限制（除权等）未计入分布。</p>}
        </section>
      )}
      <div className="ea-two">
        {tm.body && <ThemesView t={tm.body} releaseId={rel.pinnedReleaseId} />}
        {gr.body && <GrowthView g={gr.body} />}
      </div>
      {rg.body && <RegimeBanner r={rg.body} />}
      <TapePanel />
      {data && data.capability_gaps.length > 0 && (
        <details className="small card">
          <summary>数据缺口与口径说明（{data.capability_gaps.length}）</summary>
          <ul>{data.capability_gaps.map((g) => <li key={g.reason_code}><span className="mono">{g.reason_code}</span> {g.requirement}</li>)}</ul>
        </details>
      )}
    </div>
  );
}
