import { useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { History, Layers, Play } from 'lucide-react';
import { api } from '../../core/api';
import { DEFAULT_BACKTEST } from '../../core/contract';
import type { BacktestBatchResponse, BacktestRequest, BacktestResponse, StrategyDefinition } from '../../core/contract';
import { num, pct, signed, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { Bars, LineChart } from '../../kit/charts';
import { Empty, ErrorNote, Panel, Skeleton } from '../../kit/primitives';

const MONTHS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12'];

function Kpi({ label, value, note, toneValue }: { label: string; value: string; note?: string; toneValue?: number | null }) {
  return (
    <div className="kpi">
      <span>{label}</span>
      <b className={`num ${toneValue === undefined ? '' : tone(toneValue)}`}>{value}</b>
      {note && <small>{note}</small>}
    </div>
  );
}

function MonthlyGrid({ result }: { result: BacktestResponse }) {
  const years = useMemo(() => {
    const map = new Map<string, Map<string, number>>();
    result.monthly_returns.forEach(item => {
      const [year, month] = item.month.split('-');
      if (!map.has(year)) map.set(year, new Map());
      map.get(year)!.set(String(Number(month)), item.value);
    });
    return [...map.entries()];
  }, [result.monthly_returns]);
  if (!years.length) return null;
  const max = Math.max(1, ...result.monthly_returns.map(item => Math.abs(item.value)));
  return (
    <div className="monthly" role="table" aria-label="月度收益">
      <div className="monthly-row head" role="row">
        <span role="columnheader" />
        {MONTHS.map(month => <span key={month} role="columnheader">{month}月</span>)}
      </div>
      {years.map(([year, months]) => (
        <div className="monthly-row" role="row" key={year}>
          <span role="rowheader" className="num">{year}</span>
          {MONTHS.map(month => {
            const value = months.get(month);
            const strength = value == null ? 0 : Math.min(1, Math.abs(value) / max);
            const background = value == null ? undefined : value >= 0 ? `oklch(0.62 0.19 19 / ${0.1 + strength * 0.6})` : `oklch(0.66 0.13 166 / ${0.1 + strength * 0.6})`;
            return <span key={month} role="cell" className="num" style={{ background }} title={value == null ? '' : `${year}-${month}: ${pct(value)}`}>{value == null ? '' : value.toFixed(1)}</span>;
          })}
        </div>
      ))}
    </div>
  );
}

type ExitKey = 'stop_loss_pct' | 'take_profit_pct' | 'trailing_stop_pct';

function Result({ result, strategy }: { result: BacktestResponse; strategy?: StrategyDefinition }) {
  const m = result.metrics;
  const labels = result.equity.map(point => point.date.slice(5));
  const excess = result.total_return - result.benchmark_return;
  return (
    <div className="bt-result">
      <div className="bt-title">
        <div>
          <span className="eyebrow">RUN {result.run_id.slice(0, 8)} · {result.provenance.mode}</span>
          <h3>{strategy?.name ?? result.request.strategy_id} <span className="faint">×</span> <span className="num">{result.request.code}</span></h3>
        </div>
        <span className={`bt-badge ${tone(excess)}`}>超额 <b className="num">{pct(excess)}</b></span>
      </div>
      <div className="kpis">
        <Kpi label="总收益" value={pct(result.total_return)} toneValue={result.total_return} note={`基准 ${pct(result.benchmark_return)}`} />
        <Kpi label="年化" value={pct(m?.annual_return)} toneValue={m?.annual_return} note={`基准 ${pct(m?.benchmark_annual_return)}`} />
        <Kpi label="最大回撤" value={pct(result.max_drawdown)} note={m?.max_drawdown_days != null ? `水下 ${m.max_drawdown_days} 天` : undefined} />
        <Kpi label="夏普" value={num(result.sharpe_ratio, 2)} note={`Sortino ${num(m?.sortino_ratio, 2)}`} />
        <Kpi label="Calmar" value={num(m?.calmar_ratio, 2)} note={`波动 ${pct(m?.annual_volatility, 1, false)}`} />
        <Kpi label="胜率" value={pct(result.win_rate, 1, false)} note={`${result.trade_count} 笔 · 连亏 ${m?.max_consecutive_losses ?? 0}`} />
        <Kpi label="盈亏比" value={num(m?.profit_factor, 2)} note={`期望 ${pct(m?.expectancy)}`} />
        <Kpi label="交易成本" value={`${num(result.total_costs, 0)} 元`} note={`持仓 ${pct(result.exposure_pct, 0, false)} 的交易日`} />
      </div>
      <div className="bt-charts">
        <LineChart
          height={220}
          labels={labels}
          zero={0}
          format={value => `${value.toFixed(1)}%`}
          series={[
            { key: 'strategy', label: '策略', values: result.equity.map(point => point.strategy), colour: '--accent', area: true, width: 2 },
            { key: 'benchmark', label: '持有基准', values: result.equity.map(point => point.benchmark), colour: '--text-3', dashed: true },
          ]}
        />
        <LineChart
          height={96}
          labels={labels}
          format={value => `${value.toFixed(1)}%`}
          series={[{ key: 'dd', label: '回撤', values: result.equity.map(point => point.drawdown), colour: '--down', area: true }]}
        />
      </div>
      <div className="bt-lower">
        <section>
          <h4>月度收益</h4>
          <MonthlyGrid result={result} />
        </section>
        <section>
          <h4>逐笔成交 <small className="faint">{result.trades.length} 笔</small></h4>
          <div className="trades">
            <div className="trade head"><span>买入</span><span>卖出</span><span>股数</span><span>收益</span></div>
            {result.trades.slice(-60).reverse().map(trade => (
              <div className="trade" key={`${trade.entry_date}-${trade.exit_date}`}>
                <span className="num">{trade.entry_date.slice(2)}<small>{num(trade.entry_price)}</small></span>
                <span className="num">{trade.exit_date.slice(2)}<small>{num(trade.exit_price)}</small></span>
                <span className="num muted">{trade.shares}</span>
                <span className={`num ${tone(trade.return_pct)}`}>{pct(trade.return_pct)}</span>
              </div>
            ))}
          </div>
        </section>
      </div>
      <details className="assumptions">
        <summary>执行假设（{result.assumptions.length} 条）</summary>
        <ul>{result.assumptions.map(text => <li key={text}>{text}</li>)}</ul>
      </details>
      {result.provenance.mode === 'SIM' && <p className="provenance-note sim">合成日线：数字只验证代码路径，不能作为研究结论。</p>}
    </div>
  );
}

function Batch({ strategyId, form }: { strategyId: string; form: Partial<BacktestRequest> }) {
  const [result, setResult] = useState<BacktestBatchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const filters = useWorkspace(state => state.filters);
  const run = async () => {
    setBusy(true);
    try {
      const top = await api.query({ ...filters, limit: 10 });
      if (!top.records.length) {
        useWorkspace.getState().notify('当前选股条件没有命中，无法批量对比', 'error');
        return;
      }
      setResult(await api.batch({
        strategy_id: strategyId,
        codes: top.records.map(record => record.code),
        lookback: form.lookback ?? DEFAULT_BACKTEST.lookback,
        holding_days: form.holding_days ?? DEFAULT_BACKTEST.holding_days,
        commission_bps: form.commission_bps ?? DEFAULT_BACKTEST.commission_bps,
        slippage_bps: form.slippage_bps ?? DEFAULT_BACKTEST.slippage_bps,
        stamp_duty_bps: form.stamp_duty_bps ?? DEFAULT_BACKTEST.stamp_duty_bps,
        initial_cash: DEFAULT_BACKTEST.initial_cash,
      }));
    } catch (error) {
      useWorkspace.getState().notify(`批量对比失败：${error instanceof Error ? error.message : String(error)}`, 'error');
    } finally {
      setBusy(false);
    }
  };
  const rows = result?.rows.filter(row => row.error == null) ?? [];
  return (
    <Panel
      eyebrow="BATCH"
      title="批量对比"
      subtitle="同一策略与费用模型，套用到当前选股结果的前 10 只"
      actions={<button className="btn sm" onClick={run} disabled={busy}>{busy ? <span className="spinner" /> : <Layers size={13} />}运行</button>}
    >
      {result ? (
        <div className="batch">
          <div className="batch-summary">
            <span>完成 <b className="num">{result.completed}</b>/{result.requested}</span>
            <span>中位收益 <b className={`num ${tone(result.median_return)}`}>{pct(result.median_return)}</b></span>
            <span>跑赢基准 <b className="num">{result.beat_benchmark}</b> 只</span>
          </div>
          <Bars values={rows.map(row => row.excess_return)} labels={rows.map(row => row.name ?? row.code)} format={value => `${value.toFixed(1)}%`} height={150} />
          {result.rows.filter(row => row.error).map(row => <p key={row.code} className="faint batch-error">{row.code}：{row.error}</p>)}
        </div>
      ) : <p className="muted">在「问句选股」里先筛出一组标的，再来这里比较策略在它们身上的表现。</p>}
    </Panel>
  );
}

export default function BacktestPanel() {
  const catalog = useLive(signal => api.strategies(signal), [], false);
  const runs = useLive(signal => api.runs(signal), [], false);
  const [form, setForm] = useState<Partial<BacktestRequest>>(() => ({ ...DEFAULT_BACKTEST, code: sessionStorage.getItem('pulse.backtest.code') ?? DEFAULT_BACKTEST.code }));
  const [result, setResult] = useState<BacktestResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const strategies = catalog.data?.strategies ?? [];
  const strategy = strategies.find(item => item.id === form.strategy_id) ?? strategies[0];

  useEffect(() => { sessionStorage.removeItem('pulse.backtest.code'); }, []);

  const pick = (item: StrategyDefinition) => setForm(current => ({ ...current, strategy_id: item.id, lookback: item.lookback, holding_days: item.holding_days }));

  const run = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!/^\d{6}$/.test(form.code ?? '')) { setError('股票代码必须是 6 位数字'); return; }
    setBusy(true);
    setError(null);
    try {
      setResult(await api.backtest({ ...form, strategy_id: strategy?.id ?? form.strategy_id }));
      runs.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const field = (key: keyof BacktestRequest, value: number) => setForm(current => ({ ...current, [key]: value }));

  return (
    <div className="bt-layout">
      <form className="panel bt-config" onSubmit={run}>
        <div className="panel-head"><div className="panel-title"><span className="eyebrow">STRATEGY</span><h2>策略与参数</h2></div></div>
        <div className="panel-body bt-config-body">
          <div className="strategy-list" role="radiogroup" aria-label="策略">
            {catalog.data ? strategies.map(item => (
              <button type="button" key={item.id} role="radio" aria-checked={strategy?.id === item.id} className="strategy" onClick={() => pick(item)}>
                <span className="strategy-head"><strong>{item.name}</strong><span className="badge outline">{item.category}</span></span>
                <small>{item.subtitle}</small>
              </button>
            )) : <Skeleton height={200} />}
          </div>
          {strategy && <p className="strategy-method">{strategy.methodology}</p>}
          <label className="label" htmlFor="bt-code">标的代码</label>
          <div className="field">
            <input id="bt-code" className="num" inputMode="numeric" maxLength={6} value={form.code ?? ''} onChange={event => setForm(current => ({ ...current, code: event.target.value.replace(/\D/g, '') }))} />
          </div>
          <div className="bt-params">
            {([['lookback', '回看', 5, 60], ['holding_days', '持有天数', 1, 20], ['commission_bps', '佣金 bp', 0, 30], ['slippage_bps', '滑点 bp', 0, 50], ['stamp_duty_bps', '印花税 bp', 0, 20]] as [keyof BacktestRequest, string, number, number][]).map(([key, label, min, max]) => (
              <label key={key} className="param">
                <span>{label}</span>
                <input type="number" className="num" min={min} max={max} value={Number(form[key] ?? 0)} onChange={event => field(key, Number(event.target.value))} />
              </label>
            ))}
          </div>
          <span className="label">退出规则 <small className="faint">留空即不启用；T+1、止损先于止盈判定</small></span>
          <div className="bt-params">
            {([['stop_loss_pct', '止损 %', 0.5, 50], ['take_profit_pct', '止盈 %', 1, 300], ['trailing_stop_pct', '移动止损 %', 0.5, 50]] as [ExitKey, string, number, number][]).map(([key, label, min, max]) => (
              <label key={key} className="param">
                <span>{label}</span>
                <input type="number" className="num" min={min} max={max} step={0.5} placeholder="关" value={form[key] ?? ''} onChange={event => setForm(current => ({ ...current, [key]: event.target.value === '' ? null : Number(event.target.value) }))} />
              </label>
            ))}
          </div>
          <button className="btn primary bt-run" type="submit" disabled={busy || !strategy}>{busy ? <span className="spinner" /> : <Play size={14} />}运行回测</button>
          {error && <ErrorNote>{error}</ErrorNote>}
          {runs.data && runs.data.runs.length > 0 && (
            <div className="runs">
              <span className="eyebrow"><History size={11} /> 最近归档</span>
              {runs.data.runs.slice(0, 6).map(run => (
                <button type="button" key={run.run_id} className="run-row" onClick={() => api.run(run.run_id).then(setResult).catch(reason => setError(String(reason)))}>
                  <span className="num">{run.code}</span>
                  <span className="truncate muted">{strategies.find(item => item.id === run.strategy_id)?.name ?? run.strategy_id}</span>
                  <span className={`num ${tone(run.total_return)}`}>{signed(run.total_return, 1)}%</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </form>
      <div className="bt-main">
        <Panel eyebrow="TEAR SHEET" title="回测报告" subtitle="次日开盘成交 · 整手 · 双边佣金 · 卖出印花税 · 涨跌停开盘不可成交">
          {result ? <Result result={result} strategy={strategies.find(item => item.id === result.request.strategy_id)} /> : busy ? <Skeleton height={420} radius={12} /> : <Empty title="选择策略与标的后运行">结果包含净值、回撤、月度热力、逐笔成交与全部执行假设。</Empty>}
        </Panel>
        {strategy && <Batch strategyId={strategy.id} form={form} />}
      </div>
    </div>
  );
}
