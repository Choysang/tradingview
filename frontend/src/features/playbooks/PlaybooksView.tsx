import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../core/api';
import type { PlaybookSummary } from '../../core/contract';
import { pct, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { ScoreRing, Spark } from '../../kit/charts';
import { ErrorNote, Seg, Skeleton } from '../../kit/primitives';
import { HORIZON, STYLE, VERDICT, VERDICT_ORDER } from '../regime/labels';
import type { Horizon, Verdict } from '../regime/labels';
import { HistoryChip, RegimeBanner } from '../regime/RegimeBanner';
import { PageHead } from '../shell/Shell';
import PlaybookDetail from './PlaybookDetail';
import Rotation from './Rotation';
import './playbooks.css';

type HorizonFilter = Horizon | 'all';
type VerdictFilter = Verdict | 'all';

function rank(summary: PlaybookSummary) {
  return (summary.fitness ?? -1) * 10 - VERDICT_ORDER.indexOf(summary.verdict);
}

function Item({ summary, active, onSelect }: { summary: PlaybookSummary; active: boolean; onSelect: () => void }) {
  const { spec } = summary;
  const verdict = VERDICT[summary.verdict];
  const curve = summary.equity_sparkline;
  return (
    <li>
      <button className={`pb-item verdict-${summary.verdict} ${active ? 'is-active' : ''}`} aria-pressed={active} onClick={onSelect}>
        <ScoreRing value={summary.fitness} size={40} stroke={3} colour={summary.verdict === 'effective' ? '--up' : summary.verdict === 'regime_dependent' ? '--gold' : '--text-3'} label={summary.fitness == null ? '无适配度' : `适配度 ${summary.fitness.toFixed(0)}`} />
        <span className="pb-name">
          <strong>{spec.name}</strong>
          <small>{STYLE[spec.style]} · {HORIZON[spec.horizon]}{summary.signals_today ? ` · 今日 ${summary.signals_today} 只` : ''}</small>
        </span>
        <span className="pb-side">
          <span className={`badge ${verdict.badge}`}>{verdict.label}</span>
          <span className={`num ${tone(summary.out_of_sample.avg_return)}`} title="样本外每笔平均收益">{pct(summary.out_of_sample.avg_return, 2)}</span>
        </span>
        {curve.length > 1 ? <Spark values={curve} width={56} height={22} baseline={curve[0]} fill={false} label={`${spec.name} 净值`} /> : <span className="pb-nocurve faint">—</span>}
      </button>
    </li>
  );
}

export default function PlaybooksView() {
  const board = useLive(signal => api.playbooks(signal), [], false);
  const selected = useWorkspace(state => state.playbook);
  const [horizon, setHorizon] = useState<HorizonFilter>('all');
  const [verdict, setVerdict] = useState<VerdictFilter>('all');
  const detailRef = useRef<HTMLDivElement>(null);
  const all = board.data?.playbooks ?? [];
  const list = useMemo(
    () => all
      .filter(item => (horizon === 'all' || item.spec.horizon === horizon) && (verdict === 'all' || item.verdict === verdict))
      .sort((a, b) => rank(b) - rank(a)),
    [all, horizon, verdict],
  );
  const counts = useMemo(() => {
    const scoped = all.filter(item => horizon === 'all' || item.spec.horizon === horizon);
    return Object.fromEntries(VERDICT_ORDER.map(key => [key, scoped.filter(item => item.verdict === key).length])) as Record<Verdict, number>;
  }, [all, horizon]);
  const current = all.find(item => item.spec.id === selected) ?? list[0];

  useEffect(() => {
    if (!selected && list[0]) useWorkspace.getState().selectPlaybook(list[0].spec.id);
  }, [selected, list]);

  const choose = (id: string) => {
    useWorkspace.getState().selectPlaybook(id);
    if (window.matchMedia('(max-width: 1080px)').matches) detailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <div className="page-inner playbooks-page">
      <PageHead title="战法库" lead="打板接力、趋势突破、均线体系、学术因子与超跌反转，同一引擎、同一成本、同一 A 股约束下回测。样本外检验判断真假，分周期归因回答“什么行情用什么打法”。">
        {board.data && <HistoryChip provenance={board.data.provenance} />}
      </PageHead>
      {board.error && !board.data && <ErrorNote>战法回测失败：{board.error}</ErrorNote>}
      {!board.data && !board.error && (
        <div className="pb-layout" aria-busy="true">
          <div className="pb-list-wrap">{Array.from({ length: 8 }, (_, index) => <Skeleton key={index} height={58} radius={10} />)}</div>
          <div className="pb-loading">
            <Skeleton height={300} radius={16} />
            <p className="faint">正在对全部战法做全历史组合回测；结果按数据版本缓存，同步新数据后自动重算。</p>
          </div>
        </div>
      )}
      {board.data && (
        <>
          <RegimeBanner state={board.data.regime} compact eyebrow="当前周期 · 适配度按此计算" />
          <Rotation />
          <div className="pb-layout">
            <div className="pb-list-wrap">
              <Seg<HorizonFilter>
                label="周期"
                value={horizon}
                onChange={setHorizon}
                full
                options={[{ value: 'all', label: '全部' }, ...(['short', 'swing', 'long'] as Horizon[]).map(value => ({ value, label: HORIZON[value] }))]}
              />
              <div className="pb-verdicts" role="group" aria-label="结论筛选">
                <button className="chip" aria-pressed={verdict === 'all'} onClick={() => setVerdict('all')}>全部<span className="count">{Object.values(counts).reduce((a, b) => a + b, 0)}</span></button>
                {VERDICT_ORDER.filter(key => counts[key]).map(key => (
                  <button key={key} className="chip" aria-pressed={verdict === key} onClick={() => setVerdict(verdict === key ? 'all' : key)} title={VERDICT[key].hint}>
                    {VERDICT[key].label}<span className="count">{counts[key]}</span>
                  </button>
                ))}
              </div>
              <ol className="pb-list" aria-label="战法">
                {list.map(item => <Item key={item.spec.id} summary={item} active={current?.spec.id === item.spec.id} onSelect={() => choose(item.spec.id)} />)}
              </ol>
              <p className="faint pb-foot">计算于 <span className="num">{board.data.computed_at.slice(0, 16).replace('T', ' ')}</span> · <span className="num">{(board.data.execution_ms / 1000).toFixed(1)} s</span></p>
            </div>
            <div className="pb-detail-wrap" ref={detailRef}>
              {current && <PlaybookDetail key={current.spec.id} summary={current} regime={board.data.regime} />}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
