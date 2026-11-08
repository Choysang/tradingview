import { api } from '../../core/api';
import type { RotationReport, RotationStats } from '../../core/contract';
import { num, pct, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { LineChart } from '../../kit/charts';
import { ErrorNote, Panel, Skeleton } from '../../kit/primitives';
import { HORIZON } from '../regime/labels';

function StatCells({ stats, oos = false }: { stats: RotationStats; oos?: boolean }) {
  const cls = oos ? ' is-oos' : '';
  return (
    <>
      <td className={`num${cls}`}>{stats.periods}</td>
      <td className={`num${cls} ${tone(stats.excess_avg)}`}>{pct(stats.excess_avg, 2)}</td>
      <td className={`num${cls}`}>{stats.hit_rate == null ? '—' : `${num(stats.hit_rate, 0)}%`}</td>
      <td className={`num${cls}`}>{num(stats.t_stat, 2)}</td>
    </>
  );
}

/** The walk-forward election: which playbooks work right now, and whether following them has paid. */
function Body({ report }: { report: RotationReport }) {
  const elected = report.current.filter(row => row.selected);
  const others = report.current.filter(row => !row.selected && row.score != null).slice(0, 6);
  const def = report.configs.find(config => config.key === report.default_key);
  const periods = report.periods;
  return (
    <div className="rotation">
      <p className={`rotation-verdict ${report.validated ? 'is-valid' : ''}`}>{report.verdict}</p>
      <div className="rotation-grid">
        <section>
          <h4>
            当前推选{' '}
            <small className="faint">
              {report.decided_on ? `${report.decided_on} 收盘推选 · ` : ''}{def?.label ?? '—'}
              {report.next_in_days != null ? ` · ${report.next_in_days} 个交易日后重选` : ''}
            </small>
          </h4>
          {elected.length ? (
            <ol className="rotation-elected">
              {elected.map(row => (
                <li key={row.playbook_id}>
                  <button onClick={() => useWorkspace.getState().selectPlaybook(row.playbook_id)}>
                    <strong>{row.name}</strong>
                    <small>{HORIZON[row.horizon]} · <span className="num">{row.trades} 笔</span></small>
                    <b className={`num ${tone(row.avg_return)}`}>{pct(row.avg_return, 2)}</b>
                  </button>
                </li>
              ))}
            </ol>
          ) : <p className="faint">近期没有一套战法交易为正：轮换建议空仓。</p>}
          {others.length > 0 && (
            <p className="rotation-others faint">
              其后：{others.map(row => `${row.name} ${pct(row.avg_return, 1)}`).join(' · ')}
            </p>
          )}
        </section>
        <section className="rotation-chart">
          <h4>逐期检验 <small className="faint">每期推选战法的每笔平均收益 vs 全部战法一起做；{report.split_date} 以后为样本外</small></h4>
          {periods.length > 1 ? (
            <LineChart
              height={150}
              labels={periods.map(p => p.start)}
              format={value => pct(value, 1)}
              series={[
                { key: 'sel', label: '推选', values: periods.map(p => p.selected_return), colour: '--accent', width: 2 },
                { key: 'base', label: '全部', values: periods.map(p => p.baseline_return), colour: '--text-3', dashed: true },
              ]}
            />
          ) : <p className="faint">样本期数不足。</p>}
        </section>
      </div>
      <details className="rotation-configs">
        <summary>各设置对照（只用样本内选设置，样本外只检验选中的那一个）</summary>
        <div className="pb-stats-scroll">
          <table className="pb-stats">
            <thead>
              <tr>
                <th scope="col" rowSpan={2}>设置</th>
                <th scope="colgroup" colSpan={4}>样本内（至 {report.split_date}）</th>
                <th scope="colgroup" colSpan={5} className="is-oos">样本外</th>
              </tr>
              <tr>
                <th scope="col">期数</th><th scope="col">超额</th><th scope="col">跑赢</th><th scope="col">t</th>
                <th scope="col" className="is-oos">期数</th><th scope="col" className="is-oos">超额</th>
                <th scope="col" className="is-oos">跑赢</th><th scope="col" className="is-oos">t</th>
                <th scope="col" className="is-oos">有持仓超额</th>
              </tr>
            </thead>
            <tbody>
              {report.configs.map(config => (
                <tr key={config.key} className={config.key === report.default_key ? 'is-default' : ''}>
                  <th scope="row">{config.label}{config.key === report.default_key && <i> 选中</i>}</th>
                  <StatCells stats={config.in_sample} />
                  <StatCells stats={config.out_of_sample} oos />
                  <td className={`num is-oos ${tone(config.out_of_sample.invested_excess_avg)}`}>
                    {pct(config.out_of_sample.invested_excess_avg, 2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ul className="assumptions">{report.notes.map(note => <li key={note}>{note}</li>)}</ul>
      </details>
    </div>
  );
}

export default function Rotation() {
  const report = useLive(signal => api.rotation(signal), [], false);
  return (
    <Panel eyebrow="ROTATION" title="战法轮换" subtitle={`每 ${report.data?.step ?? 20} 个交易日按近期表现重新推选，只用推选日以前已平仓的交易`} className="rotation-panel">
      {report.error && !report.data && <ErrorNote>轮换计算失败：{report.error}</ErrorNote>}
      {!report.data && !report.error && <Skeleton height={180} radius={12} />}
      {report.data && <Body report={report.data} />}
    </Panel>
  );
}
