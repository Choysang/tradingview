/**
 * 问句选股 / 筛选 (PEX10): ask bar -> removable condition chips with unmatched fragments flagged, five presets, filter rail,
 * frozen screen runs (queued -> ready, three-valued rows), saved screens on the server, CSV of the whole run.
 * Why: the old site's strongest screening features, rebuilt on the typed AST. Invariants: nothing the parser could not read is
 * dropped silently (it is shown and blocks 「运行」 until acknowledged); UNKNOWN rows are shown as 未知, never as 不满足 or 0; a failed run
 * is an error state, never an empty list; saved conditions live on the server (not in localStorage).
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError } from '../../../api/client';
import { Badge, MissingText } from '../../../components/Badges';
import { MetaStrip } from '../../../components/MetaStrip';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { useToast } from '../../../components/Toast';
import { extB, explain, isNotReady, type InstrumentLabel, type ParseResult, type SavedScreen, type ScreenAst, type ScreenResultRow } from './api';
import { CATALOG, FIELD_BY_ID, RAIL_FIELDS, TRUTH_TEXT, cellOf, chipsOf, condFromChips, emptyRail, makeAst, mergeConds, newUuid, railToCond, showValue, type RailState } from './helpers';
import type { Cond } from './api';
import './ext-b.css';

const EXAMPLES = ['首板，量比大于1.5，剔除ST', '3连板以上 或 5连阳以上', '创60日新高 放量 流通市值小于200亿元', '均线多头 5日涨幅小于0', 'RSI小于30 缩量 距新高小于-20%'];
const REASON_TEXT: Record<string, string> = { unsupported: '暂不支持（数据源没有这个字段）', unit_required: '需要写明单位，不会猜', ambiguous: '含义冲突或有歧义' };
const UNIVERSE_TEXT: Record<string, string> = { 'screen-universe-v1': '全部有行情的股票', 'screen-universe-main-v1': '沪深主板' };

export function ScreenPage() {
  const toast = useToast();
  const qc = useQueryClient();
  const nav = useNavigate();
  const ctx = useQuery({ queryKey: ['extb', 'context'], queryFn: ({ signal }) => extB.context(signal), staleTime: 60_000 });
  const [text, setText] = useState('');
  const [chips, setChips] = useState<Array<{ key: string; text: string; cond: Cond }>>([]);
  const [issues, setIssues] = useState<ParseResult['unresolved']>([]);
  const [sort, setSort] = useState<ScreenAst['sort'] | undefined>(undefined);
  const [rail, setRail] = useState<RailState>(emptyRail());
  const [universe, setUniverse] = useState('screen-universe-main-v1');
  const [signals, setSignals] = useState<string[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [ackIssues, setAckIssues] = useState(false);
  const [exIdx, setExIdx] = useState(0);
  useEffect(() => { const t = setInterval(() => setExIdx((i) => (i + 1) % EXAMPLES.length), 4000); return () => clearInterval(t); }, []);

  const parse = useMutation({
    mutationFn: (t: string) => extB.parseScreenText(t),
    onSuccess: (r) => {
      const d = r.data;
      setChips(d.ast ? chipsOf(d.ast.condition) : []);
      setIssues(d.unresolved);
      setSort(d.ast?.sort);
      setAckIssues(false);
    },
    onError: (e) => toast.push('bad', `解析失败：${explain(e)}`),
  });

  const railCond = useMemo(() => railToCond(rail), [rail]);
  const signalConds: Cond[] = signals.map((pid) => ({
    node: 'compare', clock: { kind: 'bar_close', timeframe: '1d', session_policy_version: 'regular_continuous_v1' }, operator: 'eq',
    left: { kind: 'feature', feature_id: `signal.${pid}`, definition_version: CATALOG.field_version, instrument_id: CATALOG.subject_id, timeframe: '1d', field_path: 'value', unit: 'flag', resolution_policy_version: 'latest-completed-bar-v1', initial_feature_key_hash: null },
    right: { kind: 'constant', value: '1', unit: 'flag' },
  }));
  const condition = useMemo(() => mergeConds(mergeConds(condFromChips(chips), railCond), signalConds.length ? condFromChips(signalConds.map((c) => ({ cond: c }))) : null), [chips, railCond, signals]); // eslint-disable-line react-hooks/exhaustive-deps
  const ast: ScreenAst | null = condition ? { ...makeAst(condition), sort: sort ?? makeAst(condition).sort } : null;
  const blocked = issues.length > 0 && !ackIssues;

  const run = useMutation({
    mutationFn: () => {
      const c = ctx.data?.data;
      if (!c || !ast) throw new Error('缺少条件或输入快照');
      const instances = (c.playbook_instances ?? []).filter((p) => signals.includes(p.playbook_id)).map(({ instance_id, strategy_id, strategy_version }) => ({ instance_id, strategy_id, strategy_version }));
      return extB.runScreen({ ast, universe_policy_version: universe, input_snapshot_refs: c.input_snapshot_refs, decision_as_of: c.decision_as_of, strategy_instances: instances });
    },
    onSuccess: (r) => setRunId(r.data.receipt.resource_id),
    onError: (e) => toast.push('bad', `运行失败：${explain(e)}`),
  });

  const setBound = (f: string, side: 'lo' | 'hi', v: string): void =>
    setRail((r) => ({ ...r, bounds: { ...r.bounds, [f]: { lo: r.bounds[f]?.lo ?? '', hi: r.bounds[f]?.hi ?? '', [side]: v } } }));
  const toggle = (list: string[], v: string): string[] => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

  return (
    <div className="page extb">
      <SectionTitle title="问句选股" hint="用一句中文描述条件，系统按固定字段目录翻译成可检查的条件；认不出的片段会原样标出，不会静默丢掉。价格为前复权研究序列，未知的字段显示「未知」而不是 0。" />
      <form className="askbar" onSubmit={(e) => { e.preventDefault(); if (text.trim()) parse.mutate(text); }}>
        <input aria-label="问句" value={text} onChange={(e) => setText(e.target.value)} placeholder={`例如：${EXAMPLES[exIdx]}`} />
        <button className="btn btn-on" type="submit" disabled={parse.isPending || !text.trim()}>{parse.isPending ? '解析中…' : '解析'}</button>
      </form>
      <div className="chips presets" role="group" aria-label="策略预设">
        {CATALOG.presets.map((p) => (
          <button key={p.id} className="btn btn-small" title={`${p.subtitle}\n${p.note}`} onClick={() => { setText(p.text); parse.mutate(p.text); }}>{p.name}</button>
        ))}
      </div>

      {issues.length > 0 && (
        <div className="state state-error" role="alert" aria-label="未识别片段">
          <div><b>有 {issues.length} 处没有被识别（原样列出，运行前请确认）</b></div>
          <ul>{issues.map((i, k) => <li key={k}><code>{i.text}</code> — {REASON_TEXT[i.reason_code] ?? i.reason_code}</li>)}</ul>
          <label className="check"><input type="checkbox" checked={ackIssues} onChange={(e) => setAckIssues(e.target.checked)} /> 我知道这些片段不会参与筛选</label>
        </div>
      )}
      <div className="chips" aria-label="已识别条件">
        {chips.length === 0 && <span className="muted small">还没有条件：输入问句，或用下面的筛选栏添加。</span>}
        {chips.map((c) => <span key={c.key} className="chip">{c.text}<button className="chip-x" aria-label={`移除 ${c.text}`} onClick={() => setChips((x) => x.filter((y) => y.key !== c.key))}>×</button></span>)}
      </div>

      <details className="rail">
        <summary>筛选栏（区间、形态、板块、策略信号）</summary>
        <div className="rail-grid">
          {RAIL_FIELDS.map((f) => (
            <label key={f} title={FIELD_BY_ID[f]?.description}>{FIELD_BY_ID[f]?.label}<small>{FIELD_BY_ID[f]?.unit}</small>
              <span className="pair"><input aria-label={`${FIELD_BY_ID[f]?.label}下限`} inputMode="decimal" placeholder="下限" value={rail.bounds[f]?.lo ?? ''} onChange={(e) => setBound(f, 'lo', e.target.value)} />
                <input aria-label={`${FIELD_BY_ID[f]?.label}上限`} inputMode="decimal" placeholder="上限" value={rail.bounds[f]?.hi ?? ''} onChange={(e) => setBound(f, 'hi', e.target.value)} /></span></label>
          ))}
        </div>
        <div className="chips">
          <label className="check"><input type="checkbox" checked={rail.excludeSt} onChange={(e) => setRail({ ...rail, excludeSt: e.target.checked })} /> 剔除 ST</label>
          <label className="check"><input type="checkbox" checked={rail.excludeNew} onChange={(e) => setRail({ ...rail, excludeNew: e.target.checked })} /> 剔除次新（&lt;60 日）</label>
          {(['main', 'chinext', 'star', 'bse'] as const).map((b) => (
            <label key={b} className="check"><input type="checkbox" checked={rail.boards.includes(b)} onChange={() => setRail({ ...rail, boards: toggle(rail.boards, b) })} /> {FIELD_BY_ID[`board_${b}`]?.label}</label>
          ))}
        </div>
        <div className="chips" aria-label="形态">
          {CATALOG.patterns.map((p) => (
            <label key={p.id} className="check" title={p.rule}><input type="checkbox" checked={rail.patterns.includes(p.id)} onChange={() => setRail({ ...rail, patterns: toggle(rail.patterns, p.id) })} /> {p.name}</label>
          ))}
        </div>
        {(ctx.data?.data.playbook_instances.length ?? 0) > 0 && (
          <label className="stack">用战法信号筛选（研究用途：未检验的战法也可以筛选，不影响任何推荐）
            <select multiple size={6} value={signals} onChange={(e) => setSignals(Array.from(e.target.selectedOptions).map((o) => o.value))}>
              {ctx.data?.data.playbook_instances.map((p) => <option key={p.instance_id} value={p.playbook_id}>{p.display_name}</option>)}
            </select>
          </label>
        )}
      </details>

      <div className="runbar">
        <label>股票池 <select value={universe} onChange={(e) => setUniverse(e.target.value)}>{(ctx.data?.data.universe_policy_versions ?? Object.keys(UNIVERSE_TEXT)).map((u) => <option key={u} value={u}>{UNIVERSE_TEXT[u] ?? u}</option>)}</select></label>
        <button className="btn btn-on" disabled={!ast || blocked || run.isPending || !ctx.data} onClick={() => run.mutate()}>{run.isPending ? '提交中…' : '运行筛选'}</button>
        <SaveBox ast={ast} universe={universe} onLoad={(s) => { setChips(chipsOf(s.ast.condition)); setSort(s.ast.sort); setUniverse(s.universe_policy_version); setIssues([]); setRail(emptyRail()); setSignals([]); }} />
        {ctx.isError && <span className="muted small">无法读取输入快照：{explain(ctx.error)}</span>}
      </div>

      {runId ? <RunResults runId={runId} onOpen={(id) => nav(`/stock/${id}`)} onRetry={() => qc.invalidateQueries({ queryKey: ['extb', 'run', runId] })} /> : <EmptyState>设定条件后点击「运行筛选」。结果是一次冻结的扫描：同一次运行的分页顺序固定。</EmptyState>}
    </div>
  );
}

function SaveBox({ ast, universe, onLoad }: { ast: ScreenAst | null; universe: string; onLoad: (s: SavedScreen) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const [current, setCurrent] = useState<{ id: string; version: number } | null>(null);
  const list = useQuery({ queryKey: ['extb', 'saved'], queryFn: ({ signal }) => extB.listSavedScreens(50, null, signal) });
  const save = useMutation({
    mutationFn: () => {
      if (!ast || !name.trim()) throw new Error('需要名称和条件');
      const id = current?.id ?? newUuid();
      return extB.saveScreen({ screen_id: id, expected_screen_version: current?.version ?? 0, display_name: name.trim(), ast, universe_policy_version: universe }).then((r) => ({ r, id }));
    },
    onSuccess: ({ r, id }) => { setCurrent({ id, version: r.data.screen.version }); toast.push('good', `已保存到服务器（版本 ${r.data.screen.version}）`); qc.invalidateQueries({ queryKey: ['extb', 'saved'] }); },
    onError: (e) => toast.push('bad', e instanceof ApiError && e.code === 'VERSION_CONFLICT' ? '这个筛选已被改动，请重新载入再保存' : `保存失败：${explain(e)}`),
  });
  return (
    <details className="saved">
      <summary>已保存的筛选{list.data ? `（${list.data.data.screens.length}）` : ''}</summary>
      <div className="stack">
        <div className="pair"><input aria-label="筛选名称" placeholder="给当前条件起个名字" value={name} onChange={(e) => setName(e.target.value)} />
          <button className="btn btn-small" disabled={!ast || !name.trim() || save.isPending} onClick={() => save.mutate()}>{current ? '保存新版本' : '保存'}</button></div>
        {list.isLoading && <Loading />}
        {list.isError && <ErrorState error={list.error} what="已保存的筛选" onRetry={() => list.refetch()} />}
        <ul className="plain">
          {list.data?.data.screens.map((s) => (
            <li key={s.screen_id}><button className="btn btn-small btn-ghost" onClick={() => { setName(s.display_name); setCurrent({ id: s.screen_id, version: s.screen_version }); onLoad(s); }}>{s.display_name}</button> <span className="muted small">v{s.screen_version}</span></li>
          ))}
        </ul>
      </div>
    </details>
  );
}

function RunResults({ runId, onOpen, onRetry }: { runId: string; onOpen: (id: string) => void; onRetry: () => void }) {
  const [dense, setDense] = useState(false);
  const [sel, setSel] = useState(0);
  const q = useInfiniteQuery({
    queryKey: ['extb', 'run', runId],
    queryFn: ({ pageParam, signal }) => extB.queryScreenRun(runId, 50, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (p) => p.data.page.next_cursor,
    retry: (n, e) => isNotReady(e) && n < 120,
    retryDelay: 1500,
  });
  const rows: ScreenResultRow[] = q.data?.pages.flatMap((p) => p.data.rows) ?? [];
  const ids = rows.map((r) => r.instrument_id);
  const labels = useQuery({
    queryKey: ['extb', 'labels', ids.join(',').slice(0, 4000)],
    queryFn: () => extB.instrumentLabels({ ids }), enabled: ids.length > 0,
  });
  const labelOf = useMemo(() => new Map<string, InstrumentLabel>((labels.data?.data ?? []).map((l) => [l.instrument_id, l])), [labels.data]);
  const tbl = useRef<HTMLTableElement>(null);
  if (q.isPending || (q.isError && isNotReady(q.error))) return <Loading label="筛选运行中…（首次加载行情面板可能需要一两分钟）" />;
  if (q.isError) return <ErrorState error={q.error} what="筛选结果" onRetry={onRetry} />;
  const first = q.data.pages[0]!;
  const cov = first.meta.coverage;
  const fields = rows[0]?.features.map((f) => f.feature_id) ?? [];
  const onKey = (e: React.KeyboardEvent): void => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, rows.length - 1)); }
    if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
    if (e.key === 'Enter' && rows[sel]) onOpen(rows[sel]!.instrument_id);
  };
  return (
    <section aria-label="筛选结果">
      <div className="counts">
        <Badge tone="good">满足 {cov.observed_count}</Badge>
        <Badge tone={cov.missing_count ? 'warn' : 'neutral'} title="因数据缺失无法判断的股票，不当作不满足">未知 {cov.missing_count ?? 0}</Badge>
        <Badge>共评估 {cov.expected_count ?? '—'}</Badge>
        <a className="btn btn-small" href={extB.csvUrl(runId)} download>导出 CSV（全部结果）</a>
        <button className="btn btn-small btn-ghost" onClick={() => setDense((d) => !d)}>{dense ? '标准行距' : '紧凑行距'}</button>
      </div>
      {first.data.capability_gaps.length > 0 && (
        <div className="state state-error" role="status">无法评估的部分：{first.data.capability_gaps.map((g) => `${FIELD_BY_ID[g.requirement]?.label ?? g.requirement}（${g.reason_code}）`).join('、')}</div>
      )}
      <MetaStrip meta={first.meta} title="筛选运行" hideCoverage />
      {rows.length === 0 ? <EmptyState>没有满足条件、也没有未知的股票。</EmptyState> : (
        <div className="scroll" tabIndex={0} onKeyDown={onKey} aria-label="结果表（↑↓ 选择，Enter 打开个股）">
          <table ref={tbl} className={`table results${dense ? ' dense' : ''}`}>
            <thead><tr><th>#</th><th>代码</th><th>名称</th><th>条件</th>{fields.map((f) => <th key={f}>{FIELD_BY_ID[f]?.label ?? f}</th>)}</tr></thead>
            <tbody>
              {rows.map((r, i) => {
                const l = labelOf.get(r.instrument_id);
                return (
                  <tr key={r.instrument_id} className={i === sel ? 'sel' : ''} onClick={() => setSel(i)} onDoubleClick={() => onOpen(r.instrument_id)}>
                    <td>{r.sort_position}</td>
                    <td><Link to={`/stock/${r.instrument_id}`}>{l?.code ?? r.instrument_id.slice(0, 8)}</Link></td>
                    <td>{l?.name ?? <MissingText>—</MissingText>}{l?.is_risk_warning ? <Badge tone="bad">ST</Badge> : null}</td>
                    <td><Badge tone={r.condition_value === 'true' ? 'good' : 'warn'}>{TRUTH_TEXT[r.condition_value]}</Badge></td>
                    {r.features.map((f) => { const c = cellOf(f.result.artifact?.ref); return <td key={f.feature_id} className="num">{c.value === null ? <MissingText /> : showValue(c)}</td>; })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {q.hasNextPage && <button className="btn" disabled={q.isFetchingNextPage} onClick={() => q.fetchNextPage()}>{q.isFetchingNextPage ? '加载中…' : '加载更多'}</button>}
    </section>
  );
}
