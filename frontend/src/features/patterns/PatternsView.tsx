import { useMemo, useState } from 'react';
import { ExternalLink, Radar } from 'lucide-react';
import { api } from '../../core/api';
import type { PatternFamily, PatternSummary } from '../../core/contract';
import { num, pct, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { Empty, ErrorNote, Panel, Seg, Skeleton } from '../../kit/primitives';
import { PageHead } from '../shell/Shell';
import './patterns.css';

type FamilyFilter = PatternFamily | 'all';

const FAMILY: Record<PatternFamily, string> = {
  trend: '趋势',
  volume: '量能',
  breakout: '突破',
  reversal: '反转',
  volatility: '波动',
};

function PatternCard({ summary, active, onSelect, index }: { summary: PatternSummary; active: boolean; onSelect: () => void; index: number }) {
  const { pattern } = summary;
  return (
    <article className={`pattern-card family-${pattern.family} ${active ? 'is-active' : ''} ${summary.hit_count ? '' : 'is-quiet'}`} style={{ '--i': index } as React.CSSProperties}>
      <button className="pattern-hit-area" onClick={onSelect} aria-pressed={active} aria-label={`${pattern.name}，今日命中 ${summary.hit_count} 只`} />
      <header>
        <span className="family-tag">{FAMILY[pattern.family]}</span>
        <span className="pattern-count num">{summary.hit_count}</span>
      </header>
      <h3>{pattern.name}</h3>
      <p className="pattern-rule">{pattern.rule}</p>
      <ul className="pattern-top">
        {summary.hits.slice(0, 3).map(hit => <li key={hit.code}><b>{hit.name}</b><span className={`num ${tone(hit.pct_chg)}`}>{pct(hit.pct_chg)}</span></li>)}
        {!summary.hits.length && <li className="faint">今日无命中</li>}
      </ul>
      <footer>
        {pattern.source_url ? (
          <a href={pattern.source_url} target="_blank" rel="noreferrer" className="pattern-source" onClick={event => event.stopPropagation()}>{pattern.source}<ExternalLink size={11} /></a>
        ) : <span className="pattern-source">{pattern.source}</span>}
      </footer>
    </article>
  );
}

function Hits({ summary }: { summary: PatternSummary }) {
  return (
    <Panel eyebrow="HITS" title={summary.pattern.name} subtitle={`${summary.hit_count} 只命中 · 按强度排序 · 依据最后一根日线`} className="hits-panel">
      <p className="hits-rule">{summary.pattern.rule}</p>
      {summary.hits.length ? (
        <ol className="hits">
          {summary.hits.map((hit, index) => (
            <li key={hit.code}>
              <button onClick={() => useWorkspace.getState().openStock(hit.code)}>
                <span className="hit-rank num">{index + 1}</span>
                <span className="hit-name"><strong>{hit.name}</strong><small className="num faint">{hit.code} · {hit.date}</small></span>
                <span className="hit-note">{hit.note}</span>
                <span className="hit-side">
                  <span className={`num ${tone(hit.pct_chg)}`}>{pct(hit.pct_chg)}</span>
                  {hit.strength != null && <small className="num faint">强度 {num(hit.strength, 2)}</small>}
                </span>
              </button>
            </li>
          ))}
        </ol>
      ) : <Empty title="今日无命中">规则没有放宽；零命中也是有效结果。</Empty>}
    </Panel>
  );
}

export default function PatternsView() {
  const scan = useLive(signal => api.patterns(signal), [], false);
  const [family, setFamily] = useState<FamilyFilter>('all');
  const [selected, setSelected] = useState<string | null>(null);
  const patterns = useMemo(() => (scan.data?.patterns ?? []).filter(item => family === 'all' || item.pattern.family === family), [scan.data, family]);
  const current = patterns.find(item => item.pattern.id === selected) ?? [...patterns].sort((a, b) => b.hit_count - a.hit_count)[0];
  const provenance = scan.data?.provenance;

  return (
    <div className="page-inner">
      <PageHead title="形态雷达" lead="把 Sequoia、InStock 等开源项目里的经典 A 股选股形态写成可解释的规则卡，按最后一根日线扫描全市场。">
        {provenance && (
          <span className={`scan-meta ${provenance.mode === 'SIM' ? 'sim' : ''}`}>
            <Radar size={14} />扫描 <b className="num">{scan.data?.scanned}</b>/<span className="num">{scan.data?.universe}</span> 只 · <span className="num">{scan.data?.execution_ms.toFixed(0)} ms</span> · {provenance.mode === 'SIM' ? '合成日线' : provenance.source}
          </span>
        )}
      </PageHead>
      <Seg<FamilyFilter>
        label="形态类别"
        value={family}
        onChange={setFamily}
        options={[{ value: 'all', label: '全部' }, ...Object.entries(FAMILY).map(([value, label]) => ({ value: value as PatternFamily, label }))]}
      />
      {scan.error && !scan.data && <ErrorNote>形态扫描失败：{scan.error}</ErrorNote>}
      {!scan.data && !scan.error && (
        <div className="patterns-layout">
          <div className="pattern-grid">{Array.from({ length: 6 }, (_, index) => <Skeleton key={index} height={220} radius={14} />)}</div>
          <Skeleton height={480} radius={14} />
        </div>
      )}
      {scan.data && (
        <div className="patterns-layout">
          <div className="pattern-grid">
            {patterns.map((summary, index) => (
              <PatternCard key={summary.pattern.id} summary={summary} index={index} active={current?.pattern.id === summary.pattern.id} onSelect={() => setSelected(summary.pattern.id)} />
            ))}
          </div>
          {current && <Hits summary={current} />}
        </div>
      )}
      {provenance?.mode === 'SIM' && <p className="faint patterns-note">SIM 模式使用合成日线，命中结果只用于验证规则与界面，不构成研究结论。</p>}
    </div>
  );
}
