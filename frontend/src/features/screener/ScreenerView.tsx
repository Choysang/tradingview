import { useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowRight, Bookmark, CornerDownLeft, Download, Rows3, Rows4, SlidersHorizontal, Sparkles, X } from 'lucide-react';
import { api } from '../../core/api';
import type { FilterInput } from '../../core/api';
import type { ParseResponse, ScreenerFilterRequest } from '../../core/contract';
import { useDebounced, useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { Empty, ErrorNote } from '../../kit/primitives';
import { PageHead } from '../shell/Shell';
import { conditions, SORT_OPTIONS } from './conditions';
import FilterRail from './FilterRail';
import ResultTable, { toCsv } from './ResultTable';
import './screener.css';

const EXAMPLES = [
  '2连板以上 换手低于15% 自由流通小于100亿',
  '早盘封板 硬板 封成比大于10%',
  '5连阳 放量 接近60日新高',
  '创业板 涨幅大于5% 成交额大于5亿',
  '小盘 首板 换手10%-30%',
];

function useRotating(items: string[], every = 3600) {
  const [index, setIndex] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setIndex(value => (value + 1) % items.length), every);
    return () => window.clearInterval(timer);
  }, [items.length, every]);
  return items[index];
}

function AskBar() {
  const ask = useWorkspace(state => state.ask);
  const filters = useWorkspace(state => state.filters);
  const [text, setText] = useState(ask);
  const [busy, setBusy] = useState(false);
  const [parsed, setParsed] = useState<ParseResponse | null>(null);
  const [focused, setFocused] = useState(false);
  const placeholder = useRotating(EXAMPLES);
  const active = conditions(filters);

  useEffect(() => setText(ask), [ask]);

  const run = async (value: string) => {
    const question = value.trim();
    if (!question) return;
    setBusy(true);
    try {
      const result = await api.parse(question);
      setParsed(result);
      useWorkspace.getState().replaceFilters(result.filters as FilterInput, question);
      if (!result.tokens.length) useWorkspace.getState().notify('没有识别到可用条件，换个说法试试', 'error');
    } catch (error) {
      useWorkspace.getState().notify(`问句解析失败：${error instanceof Error ? error.message : String(error)}`, 'error');
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(text);
  };

  return (
    <div className={`ask ${focused ? 'is-focused' : ''} ${busy ? 'is-busy' : ''}`}>
      <form className="ask-form" onSubmit={submit} role="search">
        <Sparkles size={18} className="ask-icon" aria-hidden="true" />
        <label htmlFor="ask-input" className="sr-only">用一句话描述选股条件</label>
        <input
          id="ask-input"
          value={text}
          onChange={event => setText(event.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder={placeholder}
          autoComplete="off"
          spellCheck={false}
          maxLength={200}
        />
        {text && <button type="button" className="btn ghost icon sm" aria-label="清空问句" onClick={() => setText('')}><X size={14} /></button>}
        <button type="submit" className="btn primary ask-go" disabled={busy || !text.trim()}>
          {busy ? <span className="spinner" /> : <CornerDownLeft size={14} />}
          <span>解析并筛选</span>
        </button>
      </form>
      <div className="ask-chips" aria-live="polite">
        {active.length ? (
          <>
            {active.map((condition, index) => (
              <button key={condition.key} className="cond" style={{ '--i': index } as React.CSSProperties} onClick={() => useWorkspace.getState().setFilters(condition.clear)} aria-label={`移除条件 ${condition.label}`}>
                {condition.label}
                <X size={11} />
              </button>
            ))}
            <button className="cond-clear" onClick={() => { useWorkspace.getState().replaceFilters({}); setText(''); setParsed(null); }}>清空全部</button>
          </>
        ) : (
          <span className="ask-hint">
            试试：{EXAMPLES.slice(0, 3).map(example => (
              <button key={example} className="ask-example" onClick={() => { setText(example); void run(example); }}>{example}</button>
            ))}
          </span>
        )}
        {parsed?.unmatched.map(fragment => (
          <span key={fragment} className="cond unmatched" title="这部分没有对应的筛选字段，已忽略">未识别：{fragment}</span>
        ))}
      </div>
    </div>
  );
}

function Presets() {
  const catalog = useLive(signal => api.strategies(signal), [], false);
  const presets = useWorkspace(state => state.presets);
  const filters = useWorkspace(state => state.filters);
  const ask = useWorkspace(state => state.ask);
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState('');

  const save = (event: FormEvent) => {
    event.preventDefault();
    const label = name.trim();
    if (!label) return;
    useWorkspace.setState(state => ({ presets: [...state.presets.filter(item => item.name !== label), { id: crypto.randomUUID(), name: label, filters, ask }] }));
    useWorkspace.getState().notify(`已保存预设：${label}`, 'ok');
    setNaming(false);
    setName('');
  };

  return (
    <div className="presets" aria-label="策略预设">
      {catalog.data?.strategies.map(strategy => (
        <button key={strategy.id} className="preset" onClick={() => useWorkspace.getState().replaceFilters(strategy.filters as FilterInput, strategy.name)} title={strategy.description}>
          <span className="preset-cat">{strategy.category}</span>
          <strong>{strategy.name}</strong>
          <small className="truncate">{strategy.subtitle}</small>
        </button>
      ))}
      {presets.map(preset => (
        <span key={preset.id} className="preset mine">
          <button onClick={() => useWorkspace.getState().replaceFilters(preset.filters, preset.ask ?? '')}>
            <span className="preset-cat"><Bookmark size={10} /> 我的</span>
            <strong>{preset.name}</strong>
            <small>{conditions(preset.filters).length} 个条件</small>
          </button>
          <button className="preset-remove" aria-label={`删除预设${preset.name}`} onClick={() => useWorkspace.setState(state => ({ presets: state.presets.filter(item => item.id !== preset.id) }))}><X size={12} /></button>
        </span>
      ))}
      {naming ? (
        <form className="preset preset-new" onSubmit={save}>
          <input autoFocus value={name} onChange={event => setName(event.target.value)} placeholder="预设名称" maxLength={20} onBlur={() => !name && setNaming(false)} aria-label="预设名称" />
          <button className="btn sm primary" type="submit">保存</button>
        </form>
      ) : (
        <button className="preset preset-add" onClick={() => setNaming(true)} disabled={!conditions(filters).length}>
          <strong>＋ 保存当前</strong>
          <small>{conditions(filters).length} 个条件</small>
        </button>
      )}
    </div>
  );
}

export default function ScreenerView() {
  const filters = useWorkspace(state => state.filters);
  const density = useWorkspace(state => state.density);
  // On narrow screens the rail stacks above the table; start collapsed so results lead.
  const [railOpen, setRailOpen] = useState(() => window.matchMedia('(min-width: 1081px)').matches);
  const settled = useDebounced(filters, 160);
  const request = useMemo<FilterInput>(() => ({ ...settled, limit: 600 }), [settled]);
  const result = useLive(signal => api.query(request, signal), [request]);
  const resultsRef = useRef<HTMLDivElement>(null);
  const sortBy = (filters.sort_by ?? 'composite_score') as ScreenerFilterRequest['sort_by'];
  const sortDesc = filters.sort_desc ?? true;

  const onSort = (key: ScreenerFilterRequest['sort_by']) => {
    useWorkspace.getState().setFilters(key === sortBy ? { sort_desc: !sortDesc } : { sort_by: key, sort_desc: true });
  };

  const exportCsv = () => {
    if (!result.data) return;
    const blob = new Blob([toCsv(result.data.records)], { type: 'text/csv;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `pulse-screen-${result.data.provenance.trade_date}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  };

  return (
    <div className="page-inner screener">
      <PageHead title="问句选股" lead="用一句中文描述条件，解析结果会变成下方可逐条移除的条件；也可以在左侧精调每个字段。" />
      <AskBar />
      <Presets />
      <div className={`screener-body ${railOpen ? '' : 'rail-closed'}`} ref={resultsRef}>
        <aside className="screener-rail panel" aria-label="筛选条件">
          <div className="rail-head">
            <span className="eyebrow">FILTERS</span>
            <button className="btn ghost sm" onClick={() => setRailOpen(false)} aria-label="收起筛选栏"><SlidersHorizontal size={13} />收起</button>
          </div>
          <FilterRail filters={filters} onChange={patch => useWorkspace.getState().setFilters(patch)} />
        </aside>
        <section className="results panel" aria-label="筛选结果">
          <header className="results-head">
            {!railOpen && <button className="btn sm" onClick={() => setRailOpen(true)}><SlidersHorizontal size={13} />筛选</button>}
            <div className="results-count">
              <strong className="num">{result.data?.total_matched ?? '—'}</strong>
              <span>只命中</span>
              {result.data && <span className="faint num">· {result.data.execution_ms.toFixed(1)} ms</span>}
              {result.loading && <span className="spinner" aria-label="更新中" />}
            </div>
            <div className="results-actions">
              <label className="field sort-field">
                <span className="sr-only">排序</span>
                <select value={sortBy} onChange={event => useWorkspace.getState().setFilters({ sort_by: event.target.value as ScreenerFilterRequest['sort_by'], sort_desc: true })}>
                  {SORT_OPTIONS.map(([value, label]) => <option key={value} value={value}>按{label}</option>)}
                </select>
              </label>
              <button className="btn icon sm" onClick={() => useWorkspace.setState({ density: density === 'compact' ? 'comfortable' : 'compact' })} data-tip={density === 'compact' ? '舒展行高' : '紧凑行高'} aria-label="切换行高">
                {density === 'compact' ? <Rows3 size={14} /> : <Rows4 size={14} />}
              </button>
              <button className="btn sm" onClick={exportCsv} disabled={!result.data?.records.length}><Download size={13} />CSV</button>
            </div>
          </header>
          {result.error && !result.data ? (
            <div className="panel-body"><ErrorNote>筛选失败：{result.error}</ErrorNote></div>
          ) : result.data && !result.data.records.length ? (
            <Empty title="没有标的满足全部条件">
              移除一两个条件，或<button className="text-link" onClick={() => useWorkspace.getState().replaceFilters({})}>清空全部<ArrowRight size={12} /></button>
            </Empty>
          ) : (
            <ResultTable records={result.data?.records ?? []} sortBy={sortBy} sortDesc={sortDesc} onSort={onSort} loading={result.loading} />
          )}
          {result.data && result.data.total_matched > result.data.records.length && (
            <p className="results-foot faint">显示前 {result.data.records.length} 只；收紧条件可看到全部。</p>
          )}
        </section>
      </div>
    </div>
  );
}
