/**
 * 因子与形态 (PEX10 patterns as screens, factor lab as registered experiments, PEX04 analogs and interval statistics).
 * Why: the old pattern radar (12 rules), factor lab (6 factors with rank IC / ICIR / quantile returns / long-short), analogs and
 * range statistics come back on the shared, point-in-time features. Pattern hits ARE screens (one frozen run per rule); factor
 * results are experiment metrics read through the same evaluation contract as every strategy; analogs are an EMPIRICAL
 * distribution of what followed similar setups (never a calibrated probability); a user-chosen interval is exploratory.
 */
import { useQueries, useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { currentReleaseQuery } from '../../../api/keys';
import { ops } from '../../../api/ops';
import type { Strategy } from '../../../api/types';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { extB, isNotReady, type AnalogNeighbor, type ScreenAst } from './api';
import { CATALOG, allOf, cellOf, compare, dayInstant, makeAst, metricPct, outcomeSummary, pct, showValue } from './helpers';
import './ext-b.css';

type Tab = 'patterns' | 'factors' | 'analogs' | 'interval';
const TABS: Array<[Tab, string]> = [['patterns', '形态雷达'], ['factors', '因子检验'], ['analogs', '相似情景'], ['interval', '区间统计']];

export function LabPage() {
  const [tab, setTab] = useState<Tab>('patterns');
  return (
    <div className="page extb">
      <SectionTitle title="因子与形态" hint="形态、因子、相似情景都建立在同一套共享特征（按可得时点读取）之上。这里的数字是研究描述，不是预测、也不是已验证的盈利能力。" />
      <div className="segmented" role="tablist">
        {TABS.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}</button>)}
      </div>
      {tab === 'patterns' && <Patterns />}
      {tab === 'factors' && <Factors />}
      {tab === 'analogs' && <Analogs />}
      {tab === 'interval' && <Interval />}
    </div>
  );
}

// ---- pattern radar -------------------------------------------------------------------------------------------------------
function Patterns() {
  const ctx = useQuery({ queryKey: ['extb', 'context'], queryFn: ({ signal }) => extB.context(signal), staleTime: 60_000 });
  const [picked, setPicked] = useState<string | null>(null);
  const c = ctx.data?.data;
  const runs = useQueries({
    queries: CATALOG.patterns.map((p) => ({
      queryKey: ['extb', 'pattern', p.id, c?.input_snapshot_refs[0]?.content_hash, c?.decision_as_of],
      enabled: !!c,
      staleTime: 300_000,
      retry: (n: number, e: unknown) => isNotReady(e) && n < 120,
      retryDelay: 2000,
      queryFn: async () => {
        const ast: ScreenAst = makeAst(allOf(compare(`pattern.${p.id}`, 'eq', 1), compare('is_st', 'eq', 0)));
        const r = await extB.runScreen({ ast, universe_policy_version: 'screen-universe-main-v1', input_snapshot_refs: c!.input_snapshot_refs, decision_as_of: c!.decision_as_of, strategy_instances: [] },
          `pattern-${p.id}-${c!.input_snapshot_refs[0]?.content_hash.slice(0, 12)}-${c!.decision_as_of}`);
        const id = r.data.receipt.resource_id;
        const page = await extB.queryScreenRun(id, 30);
        return { id, page };
      },
    })),
  });
  if (ctx.isLoading) return <Loading />;
  if (ctx.isError) return <ErrorState error={ctx.error} what="输入快照" onRetry={() => ctx.refetch()} />;
  const sel = picked ? runs[CATALOG.patterns.findIndex((p) => p.id === picked)] : undefined;
  return (
    <>
      <p className="muted small">每条规则是一次冻结的筛选（最新完成交易日，已剔除 ST）。「0 只」是诚实的答案；历史不足的股票记为未知，不算没命中。</p>
      <div className="cards">
        {CATALOG.patterns.map((p, i) => {
          const r = runs[i];
          const cov = r?.data?.page.meta.coverage;
          return (
            <button key={p.id} className={`card${picked === p.id ? ' on' : ''}`} onClick={() => setPicked(p.id)} aria-pressed={picked === p.id}>
              <b>{p.name}</b>
              <span className="count">{r?.isLoading ? '扫描中…' : r?.isError ? '失败' : `${cov?.observed_count ?? 0} 只`}</span>
              {cov?.missing_count ? <span className="muted small">另有 {cov.missing_count} 只历史不足（未知）</span> : null}
              <span className="muted small">{p.rule}</span>
              {p.omitted && <span className="muted small">省略：{p.omitted}</span>}
            </button>
          );
        })}
      </div>
      {sel?.data && <PatternHits id={sel.data.id} page={sel.data.page.data.rows} name={CATALOG.patterns.find((p) => p.id === picked)?.name ?? ''} />}
    </>
  );
}
function PatternHits({ id, page, name }: { id: string; page: Array<{ instrument_id: string; condition_value: string; features: Array<{ feature_id: string; result: { artifact: { ref: string } | null } }> }>; name: string }) {
  const hits = page.filter((r) => r.condition_value === 'true');
  const labels = useQuery({ queryKey: ['extb', 'plabels', id], queryFn: () => extB.instrumentLabels({ ids: hits.map((h) => h.instrument_id) }), enabled: hits.length > 0 });
  const by = new Map((labels.data?.data ?? []).map((l) => [l.instrument_id, l]));
  if (hits.length === 0) return <EmptyState>{name}：今天没有命中。</EmptyState>;
  return (
    <table className="table"><caption>{name} 命中（前 {hits.length} 只，按涨幅降序） · <a href={extB.csvUrl(id)} download>导出 CSV</a></caption>
      <thead><tr><th>代码</th><th>名称</th><th>涨幅%</th></tr></thead>
      <tbody>{hits.map((h) => { const l = by.get(h.instrument_id); const pc = h.features.find((f) => f.feature_id === 'pct_chg'); return (
        <tr key={h.instrument_id}><td><Link to={`/stock/${h.instrument_id}`}>{l?.code ?? h.instrument_id.slice(0, 8)}</Link></td><td>{l?.name ?? '—'}</td><td className="num">{pc ? showValue(cellOf(pc.result.artifact?.ref)) : <MissingText />}</td></tr>); })}</tbody>
    </table>
  );
}

// ---- factor lab -----------------------------------------------------------------------------------------------------------
function Factors() {
  const reg = useQuery({ queryKey: ['extb', 'factors'], queryFn: ({ signal }) => ops.listStrategies({ method_family: 'factor', role: 'selection' }, { signal }) });
  const mine = useMemo(() => (reg.data?.data.items ?? []).filter((s) => (s.specification as { factor?: { id?: string } }).factor?.id), [reg.data]);
  if (reg.isLoading) return <Loading />;
  if (reg.isError) return <ErrorState error={reg.error} what="因子登记库" onRetry={() => reg.refetch()} />;
  if (mine.length === 0) return <EmptyState>还没有登记因子：运行 <code>uv run python -m pulse.workers.research ext-b-register</code>。</EmptyState>;
  return <div className="stack">{mine.map((s) => <FactorCard key={s.strategy_id} s={s} />)}
    <p className="muted small">逐日 Spearman 秩相关；未来 N 日收益相邻日重叠，t 值偏乐观；未扣成本、未考虑涨跌停，未做行业/市值中性化；训练与验证时段分开列出，盲测时段未读取。</p></div>;
}
function FactorCard({ s }: { s: Strategy }) {
  const ver = useQuery({ queryKey: ['extb', 'ver', s.strategy_id, s.strategy_version], queryFn: () => extB.queryStrategyVersion(s.strategy_id, s.strategy_version) });
  const id = ver.data?.data.evaluation_ids.at(-1);
  const ev = useQuery({ queryKey: ['extb', 'eval', id], queryFn: ({ signal }) => ops.getEvaluation(id as string, { signal }), enabled: !!id });
  const meta = CATALOG.factors.find((f) => f.id === (s.specification as { factor?: { id?: string } }).factor?.id);
  const m = (name: string, sl: string): string | null | undefined => ev.data?.data.metrics.find((x) => x.metric_name === name && x.slice_key === sl)?.value;
  return (
    <section className="factor">
      <h3>{meta?.name ?? s.display_name} <Badge>未检验</Badge></h3>
      <p className="muted small">{meta?.description}</p>
      {ver.isLoading || ev.isLoading ? <Loading /> : !id ? <EmptyState>尚无实验：<code>ext-b-run --ids FL:{meta?.id}</code></EmptyState> :
        ev.data?.data.run_state !== 'completed' ? <EmptyState>实验状态：{ev.data?.data.run_state}{ev.data?.data.error_code ? `（${ev.data.data.error_code}）` : ''}</EmptyState> : (
          <table className="table dense">
            <thead><tr><th>期限/时段</th><th>IC 均值</th><th>ICIR</th><th>t 值</th><th>IC&gt;0 占比</th><th>分位收益 Q1…Q5</th><th>多空</th><th>顶组换手</th><th>有效日数</th></tr></thead>
            <tbody>
              {[5, 20].flatMap((h) => ['train', 'validation'].map((sl) => (
                <tr key={`${h}${sl}`}><td>未来{h}日 · {sl === 'train' ? '训练' : '验证'}</td>
                  <td className="num">{fmt(m(`rank_ic_mean_h${h}`, sl), 4)}</td><td className="num">{fmt(m(`icir_h${h}`, sl), 2)}</td><td className="num">{fmt(m(`ic_t_stat_h${h}`, sl), 2)}</td>
                  <td className="num">{m(`ic_positive_share_h${h}`, sl) == null ? <MissingText /> : pct(Number(m(`ic_positive_share_h${h}`, sl)), 0)}</td>
                  <td><Bars values={[1, 2, 3, 4, 5].map((q) => m(`quantile_return_q${q}_h${h}`, sl))} /></td>
                  <td className="num">{metricPct(m(`long_short_return_h${h}`, sl))}</td><td className="num">{metricPct(m(`top_group_turnover_h${h}`, sl), 0)}</td><td className="num">{Number(m(`ic_days_h${h}`, sl) ?? 0)}</td></tr>
              )))}
            </tbody>
          </table>
        )}
    </section>
  );
}
const fmt = (v: string | null | undefined, d: number): React.ReactNode => (v == null ? <MissingText /> : Number(v).toFixed(d));
function Bars({ values }: { values: Array<string | null | undefined> }) {
  const nums = values.map((v) => (v == null ? null : Number(v)));
  const max = Math.max(0.0001, ...nums.map((n) => Math.abs(n ?? 0)));
  return (
    <span className="bars" role="img" aria-label={`分位收益 ${nums.map((n) => (n == null ? '缺失' : `${(n * 100).toFixed(2)}%`)).join('，')}`}>
      {nums.map((n, i) => <i key={i} className={n == null ? 'na' : n >= 0 ? 'up' : 'dn'} style={{ height: `${n == null ? 4 : Math.max(3, (Math.abs(n) / max) * 28)}px` }} title={n == null ? '缺失' : `${(n * 100).toFixed(2)}%`} />)}
    </span>
  );
}

// ---- analogs --------------------------------------------------------------------------------------------------------------
const HORIZONS = [5, 10, 20, 40];
function Analogs() {
  const [code, setCode] = useState('');
  const [h, setH] = useState(20);
  const [policy, setPolicy] = useState('analog-knn-matured-v1');
  const [go, setGo] = useState<{ code: string; h: number; policy: string } | null>(null);
  const rel = useQuery(currentReleaseQuery());
  const q = useQuery({
    queryKey: ['extb', 'analog', go, rel.data?.data.current_release_id],
    enabled: !!go && !!rel.data?.data.current_release_id,
    retry: false,
    queryFn: async () => {
      const lab = (await extB.instrumentLabels({ codes: [go!.code] })).data[0];
      if (!lab) throw new Error('找不到这个代码');
      const release = rel.data!.data.current_release_id as string;
      const key = (await extB.analogKey(lab.instrument_id, release)).data.feature_key_hash;
      const res = await extB.queryAnalogCases({ instrument_id: lab.instrument_id, release_id: release, feature_key_hash: key, selection_policy_version: `${go!.policy}:h${go!.h}` }, 100);
      return { lab, res };
    },
  });
  return (
    <>
      <form className="form" onSubmit={(e) => { e.preventDefault(); if (code.trim()) setGo({ code: code.trim(), h, policy }); }}>
        <label>股票代码<input value={code} onChange={(e) => setCode(e.target.value)} placeholder="如 600000" /></label>
        <label>向后看（交易日）<select value={h} onChange={(e) => setH(Number(e.target.value))}>{HORIZONS.map((x) => <option key={x} value={x}>{x}</option>)}</select></label>
        <label>选择法<select value={policy} onChange={(e) => setPolicy(e.target.value)}><option value="analog-knn-matured-v1">只取结果已揭晓的历史情景</option><option value="analog-knn-all-prior-v1">取全部更早情景（未到期结果留空）</option></select></label>
        <button className="btn btn-on" type="submit" disabled={!code.trim()}>查询</button>
      </form>
      {q.isFetching && <Loading />}
      {q.isError && (isNotReady(q.error)
        ? <EmptyState>这只股票的相似情景研究还没有构建（查询本身不会触发研究）。运行：<code>uv run python -m pulse.workers.research analog-study --code {go?.code} --policy {go?.policy}:h{go?.h}</code></EmptyState>
        : <ErrorState error={q.error} what="相似情景" />)}
      {q.data && <AnalogResult neighbors={q.data.res.data.neighbors} h={go!.h} />}
    </>
  );
}
function AnalogResult({ neighbors, h }: { neighbors: AnalogNeighbor[]; h: number }) {
  const s = outcomeSummary(neighbors.map((n) => n.outcome?.value ?? null));
  const labels = useQuery({ queryKey: ['extb', 'alabels', neighbors.length, neighbors[0]?.instrument_id], queryFn: () => extB.instrumentLabels({ ids: [...new Set(neighbors.map((n) => n.instrument_id))] }) });
  const by = new Map((labels.data?.data ?? []).map((l) => [l.instrument_id, l]));
  return (
    <section>
      <div className="counts">
        <Badge>邻居 {neighbors.length}</Badge><Badge tone="good">已知结果 {s.known}</Badge>{s.immature ? <Badge tone="warn">未到期 {s.immature}</Badge> : null}
        <Badge title="相似情景中后 N 日上涨的经验频率，不是校准概率">上涨经验频率 {pct(s.upShare, 0)}</Badge>
      </div>
      <p>未来 {h} 个交易日收益的经验分布：{[10, 25, 50, 75, 90].map((p) => `P${p} ${pct(s.q[p] ?? null)}`).join(' · ')}</p>
      <p className="muted small">相似情景不是预测，分布宽说明不确定性高；邻居只按特征距离选取，结果不参与选取；价格为前复权研究序列。</p>
      <table className="table dense"><thead><tr><th>#</th><th>股票</th><th>情景日期</th><th>距离</th><th>结果到期日</th><th>后{h}日收益</th></tr></thead>
        <tbody>{neighbors.slice(0, 50).map((n, i) => { const l = by.get(n.instrument_id); return (
          <tr key={`${n.instrument_id}${n.case_as_of}`}><td>{i + 1}</td><td>{l ? `${l.code} ${l.name ?? ''}` : n.instrument_id.slice(0, 8)}</td><td>{n.case_as_of.slice(0, 10)}</td><td className="num">{n.distance.toFixed(3)}</td><td>{n.outcome_end.slice(0, 10)}</td>
            <td className="num">{n.outcome ? metricPct(n.outcome.value) : <MissingText>未到期</MissingText>}</td></tr>); })}</tbody></table>
    </section>
  );
}

// ---- interval statistics ---------------------------------------------------------------------------------------------------
const METRIC_TEXT: Record<string, [string, boolean]> = {
  period_return: ['区间收益（以窗口前最后收盘为基）', true], period_return_from_first_open: ['区间收益（以首日开盘为基）', true], max_drawdown: ['最大回撤', true], daily_volatility: ['日收益波动', true],
  up_day_share: ['上涨日占比', true], best_day: ['最好一天', true], worst_day: ['最差一天', true], high_low_range: ['高低振幅', true], avg_volume_shares: ['日均成交量（股）', false], sessions: ['交易日数', false],
};
function Interval() {
  const ctx = useQuery({ queryKey: ['extb', 'context'], queryFn: ({ signal }) => extB.context(signal), staleTime: 60_000 });
  const [code, setCode] = useState('');
  const [a, setA] = useState('');
  const [b, setB] = useState('');
  const [go, setGo] = useState<{ code: string; a: string; b: string } | null>(null);
  const ref = ctx.data?.data.input_snapshot_refs[0];
  const q = useQuery({
    queryKey: ['extb', 'interval', go, ref?.snapshot_id], enabled: !!go && !!ref, retry: false,
    queryFn: async () => {
      const lab = (await extB.instrumentLabels({ codes: [go!.code] })).data[0];
      if (!lab) throw new Error('找不到这个代码');
      return extB.queryIntervalStatistics({ instrument_id: lab.instrument_id, snapshot_id: ref!.snapshot_id, window: { start: dayInstant(go!.a), end: dayInstant(go!.b) }, adjustment_mode: 'forward_adjusted', metric_policy_version: 'interval-stats-v1' });
    },
  });
  return (
    <>
      <form className="form" onSubmit={(e) => { e.preventDefault(); if (code && a && b) setGo({ code: code.trim(), a, b }); }}>
        <label>股票代码<input value={code} onChange={(e) => setCode(e.target.value)} placeholder="如 600000" /></label>
        <label>起始日<input type="date" value={a} onChange={(e) => setA(e.target.value)} /></label>
        <label>结束日（不含）<input type="date" value={b} onChange={(e) => setB(e.target.value)} /></label>
        <button className="btn btn-on" type="submit" disabled={!code || !a || !b || !ref}>计算</button>
      </form>
      <p className="muted small">口径：前复权研究序列，与图表使用同一份价格，不混入账户收益率。区间由您事后选定，所以结果只是描述（探索性），不计入任何前向战绩。</p>
      {q.isFetching && <Loading />}
      {q.isError && <ErrorState error={q.error} what="区间统计" />}
      {q.data && (
        <>
          <Badge tone="warn">探索性 · 事后选区间</Badge> <span className="muted small">快照 {q.data.data.input_snapshot.dataset_id} v{q.data.data.input_snapshot.manifest_version}</span>
          <table className="table dense"><tbody>{q.data.data.metrics.map((m) => { const [label, ratio] = METRIC_TEXT[m.name] ?? [m.name, false]; return (
            <tr key={m.name}><td>{label}</td><td className="num">{m.value == null ? <MissingText>{m.missing_reason ?? '缺失'}</MissingText> : ratio ? metricPct(m.value) : Number(m.value).toLocaleString('zh-CN')}</td><td className="muted small">样本 {m.sample_count}</td></tr>); })}</tbody></table>
        </>
      )}
    </>
  );
}
