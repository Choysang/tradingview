/**
 * 市场周期: four-dimension regime (trend, liquidity, sentiment, style) as a timeline with a shared crosshair and the
 * current regime banner. Labels are trailing (a day's label never changes when later days arrive); cells whose dimension
 * lacked history or an index are faded and called default values. Rules are shown verbatim from the server.
 */
import { useMemo, useState } from 'react';
import { Badge } from '../../../components/Badges';
import { MetaStrip } from '../../../components/MetaStrip';
import { ReleaseBar } from '../../../components/ReleaseBar';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { useReleaseSelection } from '../../../hooks/useRelease';
import './ext-a.css';
import { RegimeTimeline } from './charts';
import { useMarketToday, usePanel } from './hooks';
import type { Regime } from './types';

const WINDOWS: Array<[number, string]> = [[60, '近 60 日'], [120, '近 120 日'], [250, '近 250 日'], [0, '全部']];

export function RegimePage() {
  const rel = useReleaseSelection();
  const today = useMarketToday(rel.effectiveReleaseId);
  const { body: r, error, isLoading } = usePanel<Regime>(today.data?.data, 'regime');
  const [win, setWin] = useState(120);
  const points = useMemo(() => (r ? (win === 0 ? r.points : r.points.slice(-win)) : []), [r, win]);
  const gap = today.data?.data.capability_gaps.find((g) => g.reason_code === 'INDEX_BARS_BEHIND_SESSION');
  return (
    <div className="page ea-page">
      <ReleaseBar rel={rel} displayedReleaseId={today.data?.meta.release_id} />
      <SectionTitle title="市场周期" hint="趋势、流动性、情绪、风格四个维度各自独立判定，时间线上共用一条光标。规则是旧站规则，阈值未经校准。" />
      {(today.isLoading || isLoading) && <Loading />}
      {today.error && <ErrorState error={today.error} what="市场周期" />}
      {error && <ErrorState error={error} what="周期产物" />}
      {today.data && <MetaStrip meta={today.data.meta} title="市场周期" />}
      {today.data && today.data.data.state.length === 0 && <EmptyState>该发布包没有市场状态组件。</EmptyState>}
      {r && !r.available && <EmptyState>缺少指数日线，无法绘制（{r.reason}）。</EmptyState>}
      {r?.available && r.current && r.labels && (
        <>
          <section className="card ea-regime-banner" aria-label="当前周期">
            <div className="ea-head"><h3>当前周期</h3><Badge tone="neutral">{r.current.date}</Badge><Badge tone="warn" title={r.version}>未校准</Badge></div>
            <p className="ea-regime-label"><b>{r.current.label}</b></p>
            <ul className="small">{r.current.explanation.map((s) => <li key={s}>{s}</li>)}</ul>
            {gap && <p className="flag flag-warn">{gap.requirement}</p>}
          </section>
          <section className="card">
            <div className="ea-head">
              <h3>时间线</h3><span className="muted small">指数：{r.index?.name}{r.index?.fallback ? '（中证全指缺失，使用替代指数）' : ''}</span><span className="grow" />
              <div className="segmented small-seg" role="group" aria-label="时间窗口">
                {WINDOWS.map(([n, label]) => <button key={n} className={win === n ? 'btn-on' : ''} onClick={() => setWin(n)}>{label}</button>)}
              </div>
            </div>
            <RegimeTimeline points={points} labels={r.labels} />
          </section>
          <section className="card">
            <h3>判定规则</h3>
            <ol className="small">{r.rules.map((x) => <li key={x}>{x}</li>)}</ol>
          </section>
        </>
      )}
    </div>
  );
}
