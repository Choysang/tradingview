import { useMemo } from 'react';
import { api } from '../../core/api';
import { DEFAULT_FACTOR_LAB } from '../../core/contract';
import type { FactorLabRequest } from '../../core/contract';
import { num, pct, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { Bars, LineChart } from '../../kit/charts';
import { ErrorNote, Panel, Seg, Skeleton } from '../../kit/primitives';

function Stat({ label, value, hint, good }: { label: string; value: string; hint: string; good?: boolean | null }) {
  return (
    <div className={`fstat ${good == null ? '' : good ? 'good' : 'weak'}`}>
      <span>{label}</span>
      <b className="num">{value}</b>
      <small>{hint}</small>
    </div>
  );
}

export default function FactorPanel() {
  const stored = useWorkspace(state => state.factor);
  const request = useMemo<FactorLabRequest>(() => ({ ...DEFAULT_FACTOR_LAB, ...stored }), [stored]);
  const lab = useLive(signal => api.factors(request, signal), [request], false);
  const set = (patch: Partial<FactorLabRequest>) => useWorkspace.setState(state => ({ factor: { ...state.factor, ...patch } }));
  const data = lab.data;

  const cumulativeIc = useMemo(() => {
    let sum = 0;
    return (data?.ic_series ?? []).map(point => (point.ic == null ? null : (sum += point.ic)));
  }, [data]);

  return (
    <div className="factor-layout">
      <div className="factor-picker" role="radiogroup" aria-label="因子">
        {(data?.catalog ?? []).map(info => (
          <button key={info.key} role="radio" aria-checked={request.factor === info.key} className="factor-card" onClick={() => set({ factor: info.key })}>
            <strong>{info.label}</strong>
            <small>{info.description}</small>
          </button>
        ))}
        {!data && Array.from({ length: 6 }, (_, index) => <Skeleton key={index} height={72} radius={12} />)}
      </div>
      <div className="factor-controls">
        <Seg<string> label="前瞻天数" value={String(request.horizon)} onChange={value => set({ horizon: Number(value) })} options={[1, 5, 10, 20].map(value => ({ value: String(value), label: `${value} 日` }))} />
        <Seg<string> label="分组数" value={String(request.quantiles)} onChange={value => set({ quantiles: Number(value) })} options={[5, 10].map(value => ({ value: String(value), label: `${value} 组` }))} />
        <Seg<string> label="样本窗口" value={String(request.window)} onChange={value => set({ window: Number(value) })} options={[60, 120, 240].map(value => ({ value: String(value), label: `${value} 天` }))} />
        {lab.loading && <span className="spinner" aria-label="计算中" />}
        {data && <span className="faint num factor-meta">{data.universe} 只 × {data.days} 天 · {data.execution_ms.toFixed(0)} ms</span>}
      </div>
      {lab.error && <ErrorNote>因子检验失败：{lab.error}</ErrorNote>}
      {data ? (
        <>
          <div className="fstats">
            <Stat label="IC 均值" value={num(data.ic_mean, 3)} hint="秩相关，越偏离 0 越有预测力" good={data.ic_mean == null ? null : Math.abs(data.ic_mean) >= 0.03} />
            <Stat label="ICIR" value={num(data.icir, 2)} hint="IC 均值 / IC 标准差" good={data.icir == null ? null : Math.abs(data.icir) >= 0.5} />
            <Stat label="t 值" value={num(data.ic_t_stat, 2)} hint="|t| ≥ 2 才算统计上可辨" good={data.ic_t_stat == null ? null : Math.abs(data.ic_t_stat) >= 2} />
            <Stat label="IC > 0" value={pct(data.ic_positive_ratio, 1, false)} hint="正 IC 天数占比" />
            <Stat label="头部换手" value={pct(data.top_turnover, 1, false)} hint="顶组成分逐期变化" />
          </div>
          <div className="factor-charts">
            <Panel eyebrow="IC SERIES" title="每日 IC 与累计 IC">
              <Bars values={data.ic_series.map(point => point.ic)} labels={data.ic_series.map(point => point.date.slice(5))} format={value => value.toFixed(3)} height={130} />
              <LineChart height={120} labels={data.ic_series.map(point => point.date.slice(5))} zero={0} format={value => value.toFixed(2)} series={[{ key: 'cum', label: '累计 IC', values: cumulativeIc, colour: '--accent', area: true }]} />
            </Panel>
            <Panel eyebrow="QUANTILES" title="分组前瞻收益" subtitle={`按因子值等分 ${request.quantiles} 组，组 1 最低`}>
              <Bars values={data.quantile_returns.map(item => item.mean_return)} labels={data.quantile_returns.map(item => `Q${item.quantile}`)} format={value => `${value.toFixed(2)}%`} showValues height={180} />
            </Panel>
            <Panel eyebrow="LONG / SHORT" title="多空组合累计" subtitle="顶组减底组，每隔前瞻期取样避免重叠">
              <LineChart height={180} labels={data.long_short.map(point => point.time.slice(5))} zero={0} format={value => `${value.toFixed(1)}%`} series={[{ key: 'ls', label: '多空累计', values: data.long_short.map(point => point.value), colour: (data.long_short.at(-1)?.value ?? 0) >= 0 ? '--up' : '--down', area: true }]} />
            </Panel>
          </div>
          <ul className="factor-notes">{data.notes.map(note => <li key={note} className={tone(0)}>{note}</li>)}</ul>
        </>
      ) : !lab.error && <Skeleton height={360} radius={14} />}
    </div>
  );
}
