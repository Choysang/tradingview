import { useMemo, useState } from 'react';
import { AlertTriangle, ArrowUpRight, ChevronDown, RefreshCw, Send } from 'lucide-react';
import { navigate } from '../../app/router';
import { api } from '../../core/api';
import type { DailyReport, PlaybookWeight, StockPick, TrackRecordResponse } from '../../core/contract';
import { num, pct, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import type { PickHorizon } from '../../core/store';
import { Bars, ScoreRing } from '../../kit/charts';
import { Empty, ErrorNote, Panel, Seg, Skeleton } from '../../kit/primitives';
import { HORIZON, VERDICT } from '../regime/labels';
import { HistoryChip, RegimeBanner } from '../regime/RegimeBanner';
import { PageHead } from '../shell/Shell';
import './picks.css';

const TABS: { value: PickHorizon; label: string }[] = [
  { value: 'overall', label: '总榜' },
  { value: 'short', label: '短线' },
  { value: 'swing', label: '波段' },
  { value: 'long', label: '长线' },
];

const HORIZON_COLOUR: Record<StockPick['horizon'], string> = { short: '--up', swing: '--accent', long: '--gold' };

function distance(price: number | null, close: number | null) {
  if (price == null || close == null || !close) return null;
  return (price / close - 1) * 100;
}

function SubScores({ pick }: { pick: StockPick }) {
  const items: [string, number | null, string][] = [
    ['技术', pick.technical, '--accent'],
    ['情绪', pick.sentiment, '--up'],
    ['趋势', pick.trend, '--gold'],
  ];
  return (
    <span className="subs">
      {items.map(([label, value, colour]) => (
        <span key={label} className="sub" style={{ '--c': `var(${colour})` } as React.CSSProperties} title={`${label}分 ${value == null ? '—' : value.toFixed(0)}`}>
          <i>{label}</i>
          <span className="sub-track"><span style={{ width: `${Math.max(0, Math.min(100, value ?? 0))}%` }} /></span>
          <b className="num">{value == null ? '—' : value.toFixed(0)}</b>
        </span>
      ))}
    </span>
  );
}

function Plan({ pick }: { pick: StockPick }) {
  const stop = distance(pick.stop_loss, pick.close);
  const target = distance(pick.take_profit, pick.close);
  return (
    <span className="plan">
      <span><i>止损</i><b className="num down">{num(pick.stop_loss, 2)}</b>{stop != null && <small className="num">{pct(stop, 1)}</small>}</span>
      <span><i>目标</i><b className="num up">{num(pick.take_profit, 2)}</b>{target != null && <small className="num">{pct(target, 1)}</small>}</span>
      <span><i>持有</i><b className="num">{pick.holding_days}</b><small>日</small></span>
    </span>
  );
}

function Reasons({ pick }: { pick: StockPick }) {
  return (
    <div className="pick-detail">
      <p className="entry-note">{pick.entry_note}</p>
      {pick.exit_plan.length > 0 && (
        <div className="exit-plan-box">
          <span className="eyebrow">卖出计划</span>
          <ol className="exit-plan">{pick.exit_plan.map(line => <li key={line}>{line}</li>)}</ol>
        </div>
      )}
      <ul className="reasons">
        {pick.reasons.map(reason => (
          <li key={reason.playbook_id}>
            <button className="reason-name" onClick={() => { useWorkspace.getState().selectPlaybook(reason.playbook_id); navigate('playbooks'); }}>
              {reason.playbook_name}<ArrowUpRight size={12} />
            </button>
            <span className="reason-note">{reason.note}</span>
            <span className="reason-stats num">
              <span title="战法权重">权重 {num(reason.weight, 2)}</span>
              <span title="该战法内的信号强度分位">信号 {num(reason.signal * 100, 0)}</span>
              {reason.win_rate != null && <span title="样本外胜率">胜率 {num(reason.win_rate, 0)}%</span>}
              {reason.avg_return != null && <span title="样本外每笔平均收益" className={tone(reason.avg_return)}>每笔 {pct(reason.avg_return, 2)}</span>}
            </span>
          </li>
        ))}
      </ul>
      {pick.risk_flags.length > 0 && (
        <ul className="risk-flags">
          {pick.risk_flags.map(flag => <li key={flag}><AlertTriangle size={13} />{flag}</li>)}
        </ul>
      )}
      <div className="pick-detail-actions">
        <button className="btn sm" onClick={() => useWorkspace.getState().openStock(pick.code)}>看 K 线与相似情景</button>
      </div>
    </div>
  );
}

function Podium({ picks, showHorizon }: { picks: StockPick[]; showHorizon: boolean }) {
  return (
    <ol className="podium">
      {picks.map((pick, index) => (
        <li key={pick.code} className={`podium-card place-${index + 1}`} style={{ '--h': `var(${HORIZON_COLOUR[pick.horizon]})`, '--i': index } as React.CSSProperties}>
          <header>
            <span className="podium-rank num">{String(pick.rank).padStart(2, '0')}</span>
            {showHorizon && <span className="badge outline">{HORIZON[pick.horizon]}</span>}
            <span className="agree" title="同时给出信号的战法数">{pick.agreement} 套共振</span>
          </header>
          <div className="podium-core">
            <ScoreRing value={pick.score} size={index === 0 ? 84 : 72} stroke={index === 0 ? 5 : 4} colour={HORIZON_COLOUR[pick.horizon]} label={`综合 ${pick.score.toFixed(0)} 分`} />
            <button className="podium-name" onClick={() => useWorkspace.getState().openStock(pick.code)}>
              <strong>{pick.name}</strong>
              <small className="num">{pick.code}{pick.themes[0] ? ` · ${pick.themes[0]}` : ''}</small>
              <span className={`num ${tone(pick.pct_chg)}`}>{num(pick.close, 2)} <small>{pct(pick.pct_chg)}</small></span>
            </button>
          </div>
          <SubScores pick={pick} />
          <ul className="podium-why">
            {pick.reasons.slice(0, 2).map(reason => <li key={reason.playbook_id}><b>{reason.playbook_name}</b>{reason.note}</li>)}
          </ul>
          <Plan pick={pick} />
          {pick.risk_flags.length > 0 && <p className="podium-risk"><AlertTriangle size={12} />{pick.risk_flags.join('；')}</p>}
        </li>
      ))}
    </ol>
  );
}

function PickRow({ pick, showHorizon }: { pick: StockPick; showHorizon: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <li className={`pick ${open ? 'is-open' : ''}`} style={{ '--h': `var(${HORIZON_COLOUR[pick.horizon]})` } as React.CSSProperties}>
      <button className="pick-row" aria-expanded={open} onClick={() => setOpen(value => !value)}>
        <span className="pick-rank num">{String(pick.rank).padStart(2, '0')}</span>
        <span className="pick-id">
          <strong>{pick.name}</strong>
          <small className="num">{pick.code}{showHorizon ? ` · ${HORIZON[pick.horizon]}` : ''}{pick.themes[0] ? ` · ${pick.themes[0]}` : ''}</small>
        </span>
        <span className="pick-score">
          <b className="num">{pick.score.toFixed(0)}</b>
          <span className="score-track"><span style={{ width: `${pick.score}%` }} /></span>
        </span>
        <SubScores pick={pick} />
        <span className="pick-agree num">{pick.agreement}<small>套</small></span>
        <span className={`pick-price num ${tone(pick.pct_chg)}`}>{num(pick.close, 2)}<small>{pct(pick.pct_chg)}</small></span>
        <Plan pick={pick} />
        <span className="pick-tail">
          {pick.risk_flags.length > 0 && <span className="risk-dot" title={pick.risk_flags.join('；')}><AlertTriangle size={13} />{pick.risk_flags.length}</span>}
          <ChevronDown size={15} className="chev" />
        </span>
      </button>
      {open && <Reasons pick={pick} />}
    </li>
  );
}

/** The bear-mode explanation is one report note with this prefix (picks.bear_notes); the banner shows it. */
const BEAR_PREFIX = '熊市模式：';

/** The report names each idle horizon in a note that starts with its label (picks.idle_notes). */
const idleNotes = (notes: string[], labels: string[]): string[] =>
  notes.filter(note => labels.some(label => note.startsWith(`${label}：`)));

function Board({ report, horizon }: { report: DailyReport; horizon: PickHorizon }) {
  const picks = report[horizon];
  const showHorizon = horizon === 'overall';
  if (!picks.length) {
    const idle = idleNotes(report.notes, horizon === 'overall' ? Object.values(HORIZON) : [HORIZON[horizon]]);
    return (
      <Empty title="今天这一档观望">
        阈值没有放宽：只有在当前周期里经得起回测的战法才参与，零推荐也是结论。
        {idle.map(note => <span key={note} className="idle-note">{note}</span>)}
      </Empty>
    );
  }
  return (
    <>
      <Podium picks={picks.slice(0, 3)} showHorizon={showHorizon} />
      {picks.length > 3 && (
        <ol className="pick-list" aria-label="其余推荐">
          {picks.slice(3).map(pick => <PickRow key={`${pick.horizon}-${pick.code}`} pick={pick} showHorizon={showHorizon} />)}
        </ol>
      )}
    </>
  );
}

function Weights({ weights }: { weights: PlaybookWeight[] }) {
  const groups = (['short', 'swing', 'long'] as const).map(horizon => ({ horizon, items: weights.filter(item => item.horizon === horizon) }));
  const max = Math.max(0.01, ...weights.map(item => item.weight));
  return (
    <div className="weights">
      {groups.map(({ horizon, items }) => (
        <section key={horizon}>
          <h4>{HORIZON[horizon]} <small className="faint num">{items.length} 套</small></h4>
          {items.length ? (
            <ul>
              {items.map(item => (
                <li key={item.playbook_id}>
                  <button onClick={() => { useWorkspace.getState().selectPlaybook(item.playbook_id); navigate('playbooks'); }}>
                    <span className="weight-name">{item.name}</span>
                    <span className={`badge ${VERDICT[item.verdict].badge}`}>{VERDICT[item.verdict].label}</span>
                    <span className="weight-bar"><span style={{ width: `${(item.weight / max) * 100}%` }} /></span>
                    <b className="num">{num(item.weight, 2)}</b>
                  </button>
                </li>
              ))}
            </ul>
          ) : <p className="faint weights-none">当前周期没有达标战法</p>}
        </section>
      ))}
    </div>
  );
}

type TrackKind = 'simulated' | 'recorded';

function Track({ track }: { track: TrackRecordResponse }) {
  const [kind, setKind] = useState<TrackKind>(() => (track.summary.some(row => row.kind === 'recorded' && row.matured_picks > 0) ? 'recorded' : 'simulated'));
  const [horizon, setHorizon] = useState<PickHorizon>('overall');
  const rows = TABS.map(tab => ({ tab, row: track.summary.find(item => item.kind === kind && item.horizon === tab.value) }));
  const points = track.points.filter(point => point.kind === kind && point.horizon === horizon && point.matured > 0).slice(-60);
  return (
    <div className="track">
      <Seg<TrackKind>
        label="验证方式"
        value={kind}
        onChange={setKind}
        full
        options={[{ value: 'simulated', label: '样本外回放' }, { value: 'recorded', label: '实盘记录' }]}
      />
      <table className="track-table">
        <thead>
          <tr><th>榜单</th><th>样本</th><th>平均</th><th>胜率</th><th>超额</th></tr>
        </thead>
        <tbody>
          {rows.map(({ tab, row }) => (
            <tr key={tab.value} className={horizon === tab.value ? 'is-active' : ''}>
              <th><button onClick={() => setHorizon(tab.value)} aria-pressed={horizon === tab.value}>{tab.label}</button></th>
              <td className="num">{row?.matured_picks ?? 0}</td>
              <td className={`num ${tone(row?.avg_return)}`}>{pct(row?.avg_return, 2)}</td>
              <td className="num">{row?.hit_rate == null ? '—' : `${num(row.hit_rate, 0)}%`}</td>
              <td className={`num ${tone(row?.excess_return)}`}>{pct(row?.excess_return, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {points.length ? (
        <>
          <p className="track-caption">每期推荐相对中证全指的超额收益 <small className="faint">（{TABS.find(tab => tab.value === horizon)?.label}，最近 {points.length} 期）</small></p>
          <Bars values={points.map(point => point.excess_return)} labels={points.map(point => point.trade_date.slice(5))} height={120} format={value => pct(value, 2)} />
        </>
      ) : (
        <Empty title={kind === 'recorded' ? '还没有到期的实盘记录' : '暂无回放数据'}>
          {kind === 'recorded' ? '每份报告要等持有期结束才计入：短线 3 日、波段 10 日、长线 40 日。' : '需要更长的历史才能做样本外回放。'}
        </Empty>
      )}
    </div>
  );
}

export default function PicksView() {
  const horizon = useWorkspace(state => state.pickHorizon);
  const [date, setDate] = useState<string | null>(null);
  const [busy, setBusy] = useState<'generate' | 'push' | null>(null);
  const report = useLive(signal => api.picks(date, signal), [date], false);
  const history = useLive(signal => api.pickHistory(signal), [report.data?.generated_at], false);
  const track = useLive(signal => api.trackRecord(signal), [report.data?.generated_at], false);
  const push = useLive(signal => api.pushStatus(signal), [], false);
  const data = report.data;
  const bearNote = data?.market_mode === 'bear' ? data.notes.find(note => note.startsWith(BEAR_PREFIX)) : undefined;
  const dates = useMemo(() => (history.data?.reports ?? []).map(item => item.trade_date).slice(0, 8), [history.data]);
  const notify = useWorkspace.getState().notify;

  const regenerate = async () => {
    setBusy('generate');
    try {
      const next = await api.generatePicks();
      setDate(null);
      report.refresh();
      notify(`已按 ${next.trade_date} 收盘重新计算`, 'ok');
    } catch (error) {
      notify(`计算失败：${error instanceof Error ? error.message : String(error)}`, 'error');
    } finally {
      setBusy(null);
    }
  };

  const sendPush = async () => {
    setBusy('push');
    try {
      const status = await api.pushTest();
      notify(status.detail, 'ok');
      push.refresh();
    } catch (error) {
      notify(`推送失败：${error instanceof Error ? error.message : String(error)}`, 'error');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="page-inner picks-page">
      <PageHead title="今日推荐" lead="二十余套主流战法在同一引擎、同一成本下做全历史回测，按当前市场周期里的样本外表现加权，合成短线、波段、长线三张榜单与一张总榜。">
        {data && <HistoryChip provenance={data.provenance} />}
        <button className="btn" onClick={regenerate} disabled={busy !== null}>
          {busy === 'generate' ? <span className="spinner" /> : <RefreshCw size={14} />}{busy === 'generate' ? '计算中' : '重新计算'}
        </button>
        <button className="btn" onClick={sendPush} disabled={busy !== null || !push.data?.configured} data-tip={push.data?.detail ?? '推送状态未知'}>
          <Send size={14} />推送
        </button>
      </PageHead>

      {report.error && !data && <ErrorNote>推荐生成失败：{report.error}</ErrorNote>}
      {!data && !report.error && (
        <div className="picks-loading" aria-busy="true">
          <Skeleton height={200} radius={20} />
          <p className="faint">首次打开需要对全部战法做历史回测并合成榜单，之后按数据版本缓存。</p>
          <div className="podium">{[0, 1, 2].map(index => <Skeleton key={index} height={300} radius={16} />)}</div>
        </div>
      )}

      {data && (
        <>
          <RegimeBanner
            state={data.regime}
            eyebrow={date ? `历史报告 · ${data.trade_date}` : 'MARKET REGIME'}
            actions={<button className="btn sm ghost" onClick={() => navigate('regime')}>周期史与打法适配<ArrowUpRight size={13} /></button>}
          />
          {bearNote && (
            <div className="market-mode" role="status">
              <AlertTriangle size={16} />
              <span><strong>熊市模式</strong>{bearNote.slice(BEAR_PREFIX.length)}</span>
            </div>
          )}
          <div className="picks-layout">
            <div className="picks-main">
              <div className="picks-toolbar">
                <Seg<PickHorizon> label="榜单" value={horizon} onChange={value => useWorkspace.setState({ pickHorizon: value })} options={TABS.map(tab => ({ ...tab, label: <>{tab.label}<span className="count num">{data[tab.value].length}</span></> }))} />
                {dates.length > 1 && (
                  <div className="report-dates" role="group" aria-label="历史报告">
                    {dates.map((item, index) => {
                      const active = (date ?? data.trade_date) === item;
                      return (
                        <button key={item} className="chip" aria-pressed={active} onClick={() => setDate(index === 0 ? null : item)}>
                          <span className="num">{item.slice(5)}</span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
              <Board report={data} horizon={horizon} />
              <ul className="picks-notes">
                {data.notes.filter(note => note !== bearNote && !idleNotes([note], Object.values(HORIZON)).length).map(note => <li key={note}>{note}</li>)}
              </ul>
            </div>
            <aside className="picks-side">
              <Panel
                eyebrow="ENSEMBLE"
                title="参与加权的战法"
                subtitle={data.selection === 'rotation'
                  ? '轮换推选：近 20 日已平仓交易排序前 3，权重按近期表现（轮换已通过逐期检验）'
                  : '适配度 = 样本外表现 × 当前周期表现 × 近期表现 × 显著性（轮换未通过检验时使用）'}
              >
                <Weights weights={data.weights} />
              </Panel>
              <Panel eyebrow="TRACK RECORD" title="以史为鉴" subtitle="推荐本身的历史成绩，次日开盘买入、持有期末收盘计">
                {track.data ? <Track track={track.data} /> : track.error ? <ErrorNote>{track.error}</ErrorNote> : <Skeleton height={220} />}
                {track.data?.notes.length ? <ul className="track-notes">{track.data.notes.map(note => <li key={note}>{note}</li>)}</ul> : null}
              </Panel>
            </aside>
          </div>
        </>
      )}
    </div>
  );
}
