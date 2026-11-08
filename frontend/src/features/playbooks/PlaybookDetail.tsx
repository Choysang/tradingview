import { useState } from 'react';
import { ExternalLink, RotateCcw } from 'lucide-react';
import { api } from '../../core/api';
import type { PerformanceStats, PlaybookEvaluation, PlaybookExitStudy, PlaybookSummary, PlaybookTrade, RegimePerformance, RegimeState } from '../../core/contract';
import { num, pct, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { LineChart, ScoreRing } from '../../kit/charts';
import { Empty, ErrorNote, Skeleton } from '../../kit/primitives';
import { DIMENSIONS, HORIZON, STYLE, VERDICT } from '../regime/labels';
import RunForm from './RunForm';

const EXIT: Record<PlaybookTrade['exit_reason'], string> = {
  time: '到期', stop: '止损', target: '止盈', trailing: '移动止损', end: '期末',
  weak_open: '低开不及预期', not_limit: '断板', below_ma5: '破 5 日线', stall: '放量滞涨', fade: '冲高回落', chandelier: '吊灯止损', ebb: '情绪退潮',
  broken: '炸板', shadow: '放量长上影', bias: '乖离过大', t_out: '做T 未买回',
};

type StatRow = { key: keyof PerformanceStats; label: string; format: (value: number | null | undefined) => string; signed?: boolean };

const ROWS: StatRow[] = [
  { key: 'trades', label: '交易笔数', format: value => num(value, 0) },
  { key: 'win_rate', label: '胜率', format: value => (value == null ? '—' : `${num(value, 1)}%`) },
  { key: 'avg_return', label: '每笔平均', format: value => pct(value, 2), signed: true },
  { key: 'median_return', label: '每笔中位', format: value => pct(value, 2), signed: true },
  { key: 'profit_factor', label: '盈亏比', format: value => num(value, 2) },
  { key: 't_stat', label: 't 值', format: value => num(value, 2) },
  { key: 'annual_return', label: '年化收益', format: value => pct(value, 1), signed: true },
  { key: 'sharpe', label: '夏普', format: value => num(value, 2) },
  { key: 'max_drawdown', label: '最大回撤', format: value => pct(value, 1, false) },
  { key: 'avg_holding_days', label: '平均持有', format: value => (value == null ? '—' : `${num(value, 1)} 日`) },
  { key: 'exposure_pct', label: '仓位占用', format: value => (value == null ? '—' : `${num(value, 0)}%`) },
];

function StatsTable({ evaluation }: { evaluation: PlaybookEvaluation }) {
  const columns: { label: string; note: string; stats: PerformanceStats }[] = [
    { label: '全周期', note: `${evaluation.period_start} 起`, stats: evaluation.full },
    { label: '样本内', note: `至 ${evaluation.split_date}`, stats: evaluation.in_sample },
    { label: '样本外', note: `${evaluation.split_date} 后`, stats: evaluation.out_of_sample },
    { label: '近 60 日', note: '最近入场', stats: evaluation.recent },
  ];
  return (
    <div className="pb-stats-scroll">
      <table className="pb-stats">
        <thead>
          <tr>
            <th scope="col"><span className="sr-only">指标</span></th>
            {columns.map(column => <th key={column.label} scope="col" className={column.label === '样本外' ? 'is-oos' : ''}>{column.label}<small>{column.note}</small></th>)}
          </tr>
        </thead>
        <tbody>
          {ROWS.map(row => (
            <tr key={row.key}>
              <th scope="row">{row.label}</th>
              {columns.map(column => {
                const value = column.stats[row.key] as number | null | undefined;
                return <td key={column.label} className={`num ${row.signed ? tone(value) : ''} ${column.label === '样本外' ? 'is-oos' : ''}`}>{row.format(value)}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RegimeAttribution({ buckets, regime }: { buckets: RegimePerformance[]; regime: RegimeState }) {
  const max = Math.max(0.5, ...buckets.map(item => Math.abs(item.stats.avg_return ?? 0)));
  return (
    <div className="attribution">
      {DIMENSIONS.map(({ key, label }) => {
        const rows = buckets.filter(item => item.dimension === key);
        if (!rows.length) return null;
        return (
          <section key={key}>
            <h4>{label}</h4>
            <ul>
              {rows.map(row => {
                const value = row.stats.avg_return;
                const width = value == null ? 0 : (Math.abs(value) / max) * 50;
                const now = regime[key] === row.regime;
                const thin = row.stats.trades < 10;
                return (
                  <li key={row.regime} className={`${now ? 'is-now' : ''} ${thin ? 'is-thin' : ''}`} title={`${row.label}：${row.stats.trades} 笔，胜率 ${row.stats.win_rate == null ? '—' : num(row.stats.win_rate, 0) + '%'}，t=${num(row.stats.t_stat, 2)}`}>
                    <span className="attr-label">{row.label}{now && <i>当前</i>}</span>
                    <span className="attr-bar">
                      <span className={`attr-fill ${tone(value)}`} style={value != null && value < 0 ? { width: `${width}%`, right: '50%' } : { width: `${width}%`, left: '50%' }} />
                    </span>
                    <b className={`num ${tone(value)}`}>{pct(value, 2)}</b>
                    <small className="num">{row.stats.trades} 笔</small>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

function Trades({ trades }: { trades: PlaybookTrade[] }) {
  if (!trades.length) return <Empty title="没有成交">信号日的次日开盘全部无法成交，或规则在这段历史里从未触发。</Empty>;
  return (
    <div className="pb-trades">
      <table>
        <thead>
          <tr><th>标的</th><th>入场</th><th>离场</th><th>收益</th><th>持有</th><th>离场原因</th></tr>
        </thead>
        <tbody>
          {[...trades].reverse().map(trade => (
            <tr key={`${trade.code}-${trade.entry_date}`}>
              <td><button onClick={() => useWorkspace.getState().openStock(trade.code)}><b>{trade.name ?? trade.code}</b><small className="num">{trade.code}</small></button></td>
              <td className="num">{trade.entry_date.slice(2)}<small>{num(trade.entry_price, 2)}</small></td>
              <td className="num">{trade.exit_date.slice(2)}<small>{num(trade.exit_price, 2)}</small></td>
              <td className={`num ${tone(trade.return_pct)}`}>{pct(trade.return_pct, 2)}</td>
              <td className="num">{trade.holding_days}</td>
              <td><span className={`exit exit-${trade.exit_reason}`}>{EXIT[trade.exit_reason]}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Buy and sell rules tried on the in-sample period; the chosen one is checked out of sample. */
function ExitStudy({ id }: { id: string }) {
  const study = useLive(signal => api.playbookExits(id, signal), [id], false);
  if (study.error) return <ErrorNote>买卖法对比失败：{study.error}</ErrorNote>;
  if (!study.data) return <Skeleton height={160} radius={12} />;
  const data: PlaybookExitStudy = study.data;
  const chosen = data.variants.find(variant => variant.key === data.chosen_key);
  const verdict = data.chosen_key === 'default'
    ? '没有一种买卖法在样本内明显好过默认（夏普至少多 0.2），保持默认。'
    : data.improved_out_of_sample
      ? `样本内选出「${chosen?.label}」，样本外也好过默认：推荐使用。`
      : `样本内选出「${chosen?.label}」，但样本外没有好过默认：仍按默认执行。`;
  return (
    <>
      <p className={`exit-verdict ${data.improved_out_of_sample ? 'is-better' : ''}`}>{verdict}</p>
      <div className="pb-stats-scroll">
        <table className="pb-stats exit-table">
          <thead>
            <tr>
              <th scope="col">买卖法</th>
              <th scope="col">样本内夏普</th><th scope="col">样本内每笔</th>
              <th scope="col" className="is-oos">样本外夏普</th><th scope="col" className="is-oos">样本外每笔</th>
            </tr>
          </thead>
          <tbody>
            {data.variants.map(variant => (
              <tr key={variant.key} className={variant.key === data.chosen_key ? 'is-default' : ''}>
                <th scope="row">{variant.label}{variant.key === data.chosen_key && <i> 选中</i>}</th>
                <td className="num">{num(variant.in_sample.sharpe, 2)}</td>
                <td className={`num ${tone(variant.in_sample.avg_return)}`}>{pct(variant.in_sample.avg_return, 2)}</td>
                <td className="num is-oos">{num(variant.out_of_sample.sharpe, 2)}</td>
                <td className={`num is-oos ${tone(variant.out_of_sample.avg_return)}`}>{pct(variant.out_of_sample.avg_return, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h4 className="exit-plan-title">买入要点 <small className="faint">选中的买法</small></h4>
      <p className="exit-entry">{data.entry_note}</p>
      <h4 className="exit-plan-title">卖出要点 <small className="faint">选中的卖法，按顺序检查</small></h4>
      <ol className="exit-plan">{data.plan.map(line => <li key={line}>{line}</li>)}</ol>
    </>
  );
}

function Evaluation({ evaluation, regime }: { evaluation: PlaybookEvaluation; regime: RegimeState }) {
  const labels = evaluation.equity.map(point => point.date);
  const split = evaluation.equity.findIndex(point => point.date >= evaluation.split_date);
  return (
    <>
      <section className="pb-block">
        <h3>组合净值 <small className="faint">按仓位规则同时持有多只，含全部成本</small></h3>
        <LineChart
          height={240}
          labels={labels}
          markers={split > 0 ? [{ index: split, label: '样本外 →' }] : []}
          format={value => value.toFixed(3)}
          series={[
            { key: 'strategy', label: '战法', values: evaluation.equity.map(point => point.strategy), colour: '--accent', area: true, width: 2 },
            // No index history means no benchmark line, never a flat 100.
            ...(evaluation.equity.some(point => point.benchmark != null)
              ? [{ key: 'benchmark', label: '中证全指', values: evaluation.equity.map(point => point.benchmark), colour: '--text-3', dashed: true }]
              : []),
          ]}
        />
        <LineChart height={80} labels={labels} axis={false} format={value => pct(value, 1, false)} series={[{ key: 'dd', label: '回撤', values: evaluation.equity.map(point => point.drawdown), colour: '--down', area: true }]} />
      </section>
      <section className="pb-block">
        <h3>样本内 / 样本外 <small className="faint">前 70% 的日期用来认识战法，后 30% 只用来检验</small></h3>
        <StatsTable evaluation={evaluation} />
      </section>
      <section className="pb-block">
        <h3>分周期表现 <small className="faint">按入场当日的市场周期归因，每笔平均收益；不足 10 笔的格子变淡</small></h3>
        <RegimeAttribution buckets={evaluation.by_regime} regime={regime} />
      </section>
      <section className="pb-block">
        <h3>买卖法对比 <small className="faint">行家会选股，大师会卖股：在默认规则上每次改一条买法或卖法，样本内选、样本外验</small></h3>
        <ExitStudy id={evaluation.spec.id} />
      </section>
      <section className="pb-block">
        <h3>适配度构成</h3>
        <ul className="fitness-reasons">
          {evaluation.fitness_reasons.map(reason => <li key={reason}>{reason}</li>)}
        </ul>
      </section>
      <section className="pb-block">
        <h3>最近成交 <small className="faint num">{evaluation.trades.length} 笔</small></h3>
        <Trades trades={evaluation.trades} />
      </section>
      <section className="pb-block">
        <h3>执行假设</h3>
        <ul className="assumptions">
          {evaluation.assumptions.map(line => <li key={line}>{line}</li>)}
        </ul>
      </section>
    </>
  );
}

export default function PlaybookDetail({ summary, regime }: { summary: PlaybookSummary; regime: RegimeState }) {
  const { spec } = summary;
  const stored = useLive(signal => api.playbook(spec.id, signal), [spec.id], false);
  const [custom, setCustom] = useState<PlaybookEvaluation | null>(null);
  const evaluation = custom ?? stored.data;
  const verdict = VERDICT[evaluation?.verdict ?? summary.verdict];
  const fitness = evaluation?.fitness ?? summary.fitness;

  return (
    <article className="panel pb-detail" aria-labelledby="pb-title">
      <header className="pb-head">
        <div className="pb-head-text">
          <div className="pb-tags">
            <span className="badge outline">{STYLE[spec.style]}</span>
            <span className="badge outline">{HORIZON[spec.horizon]}</span>
            <span className={`badge ${verdict.badge}`} title={verdict.hint}>{verdict.label}</span>
            {custom && <span className="badge accent">自定义参数</span>}
          </div>
          <h2 id="pb-title" className="display">{spec.name}</h2>
          <p className="pb-summary">{spec.summary}</p>
        </div>
        <div className="pb-fitness">
          <ScoreRing value={fitness} size={92} stroke={6} colour={fitness != null && fitness >= 60 ? '--up' : fitness != null && fitness >= 40 ? '--gold' : '--text-3'} label={fitness == null ? '无适配度' : `当前周期适配度 ${fitness.toFixed(0)}`} />
          <small>当前周期适配度</small>
        </div>
      </header>
      <div className="pb-rule">
        <span className="eyebrow">RULE</span>
        <p>{spec.rule}</p>
        <footer>
          {spec.source_url ? <a href={spec.source_url} target="_blank" rel="noreferrer">{spec.source}<ExternalLink size={11} /></a> : <span>{spec.source}</span>}
          <span className="num">退出：持有 {spec.exits.holding_days} 日{spec.exits.stop_loss_pct ? ` · 止损 ${spec.exits.stop_loss_pct}%` : ''}{spec.exits.take_profit_pct ? ` · 止盈 ${spec.exits.take_profit_pct}%` : ''}{spec.exits.trailing_stop_pct ? ` · 移动止损 ${spec.exits.trailing_stop_pct}%` : ''}</span>
          <span className="num">仓位：{spec.sizing.method === 'equal' ? '等权' : '波动率倒数'} · 最多 {spec.sizing.max_positions} 只 · 每日新开 ≤ {spec.sizing.max_new_per_day}</span>
        </footer>
      </div>
      <p className={`pb-verdict verdict-${evaluation?.verdict ?? summary.verdict}`}>{evaluation?.verdict_reason ?? summary.verdict_reason}</p>
      {!spec.available && spec.unavailable_reason && <ErrorNote>{spec.unavailable_reason}</ErrorNote>}

      {stored.error && !evaluation && <ErrorNote>回测详情加载失败：{stored.error}</ErrorNote>}
      {!evaluation && !stored.error && <div className="pb-loading"><Skeleton height={240} radius={12} /><Skeleton height={180} radius={12} /></div>}
      {evaluation && summary.verdict !== 'unavailable' && <Evaluation evaluation={evaluation} regime={regime} />}

      <section className="pb-block">
        <h3>
          自定义回测 <small className="faint">换参数、换退出与仓位规则重跑；结果不写入战法库</small>
          {custom && <button className="btn sm ghost" onClick={() => setCustom(null)}><RotateCcw size={12} />恢复默认</button>}
        </h3>
        <RunForm spec={spec} disabled={!spec.available} onRun={body => api.runPlaybook(spec.id, body)} onResult={setCustom} />
      </section>
    </article>
  );
}
