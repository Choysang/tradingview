/**
 * 战法库 (52 个) 与卖法/做T回放 (PEX11 + PEX06).
 * Why: every old playbook family is back as a registered strategy definition with a pure signal function. Each one starts at
 * 「已登记 · 未检验」 — the old backtest numbers are NOT shown; a status is earned only by experiments through the unified path
 * (评价上下文 + 执行内核回放 + 用户费用口径 + x2/x3 压力 + 基准). Failed trials (missing data, errors) are shown as failures,
 * never as empty successes. The tactic tab replays exit rules / scale-out / intraday T against the SAME-inventory hold baseline.
 */
import { useMutation, useQueries, useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ops } from '../../../api/ops';
import type { Evaluation, Strategy } from '../../../api/types';
import { Badge, GradeBadge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading, SectionTitle } from '../../../components/States';
import { useToast } from '../../../components/Toast';
import { extB, explain, type ReplaySpec } from './api';
import { CATALOG, dayInstant, metricPct, type PlaybookDef } from './helpers';
import './ext-b.css';

type Tab = 'library' | 'tactics';
const HORIZON_TEXT: Record<string, string> = { short: '短线', swing: '波段', long: '长线' };
const REQ_TEXT: Record<string, string> = {
  fundamentals_pit: '需要按发布时点保存的财务数据', sector_membership_history: '需要按时点保存的板块成员', instrument_name_history: '需要证券简称历史',
  lhb_seat_trades_history: '需要龙虎榜席位成交', float_cap: '需要逐日流通市值（当前只有最新快照，历史回测会前视）',
};

export function PlaybooksPage() {
  const [tab, setTab] = useState<Tab>('library');
  return (
    <div className="page extb">
      <SectionTitle title="战法库" hint="52 个战法都只是「已登记 · 未检验」的定义：旧站的回测数字不沿用。要获得状态，必须经过统一实验路径（封存评价上下文 + 执行内核回放 + 用户费用口径及 2 倍/3 倍压力 + 基准）。" />
      <div className="segmented" role="tablist">
        <button role="tab" aria-selected={tab === 'library'} className={tab === 'library' ? 'on' : ''} onClick={() => setTab('library')}>战法库</button>
        <button role="tab" aria-selected={tab === 'tactics'} className={tab === 'tactics' ? 'on' : ''} onClick={() => setTab('tactics')}>卖法与做T回放</button>
      </div>
      {tab === 'library' ? <Library /> : <Tactics />}
    </div>
  );
}

function Library() {
  const [family, setFamily] = useState('');
  const [horizon, setHorizon] = useState('');
  const [data, setData] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const reg = useQuery({ queryKey: ['extb', 'registry'], queryFn: ({ signal }) => ops.listStrategies({ method_family: 'rule', role: 'selection' }, { signal }), staleTime: 30_000 });
  const byPlaybook = useMemo(() => {
    const m = new Map<string, Strategy>();
    for (const s of reg.data?.data.items ?? []) {
      const pid = (s.specification as { playbook?: { id?: string } }).playbook?.id;
      if (pid) m.set(pid, s);
    }
    return m;
  }, [reg.data]);
  const list = CATALOG.playbooks.filter((p) => (!family || p.family === family) && (!horizon || p.horizon === horizon) && (!data || (data === 'missing') === (p.requires.length > 0)));
  return (
    <>
      <div className="filters">
        <label>类别<select value={family} onChange={(e) => setFamily(e.target.value)}><option value="">全部（{CATALOG.playbooks.length}）</option>{Object.entries(CATALOG.families).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label>周期<select value={horizon} onChange={(e) => setHorizon(e.target.value)}><option value="">全部</option>{Object.entries(HORIZON_TEXT).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label>数据<select value={data} onChange={(e) => setData(e.target.value)}><option value="">全部</option><option value="ok">数据齐全</option><option value="missing">缺少数据</option></select></label>
      </div>
      {reg.isLoading && <Loading />}
      {reg.isError && <ErrorState error={reg.error} what="战法登记库" onRetry={() => reg.refetch()} />}
      {reg.isSuccess && byPlaybook.size === 0 && <EmptyState>登记库里还没有战法定义：先运行 <code>uv run python -m pulse.workers.research ext-b-register</code>。</EmptyState>}
      <table className="table playbooks">
        <thead><tr><th>战法</th><th>类别</th><th>周期</th><th>持有</th><th>证据</th><th>数据</th><th /></tr></thead>
        <tbody>
          {list.map((p) => {
            const s = byPlaybook.get(p.id);
            const inst = s?.instances[0];
            return (
              <FragmentRow key={p.id} p={p} s={s} lifecycle={inst?.lifecycle} open={open === p.id} onToggle={() => setOpen(open === p.id ? null : p.id)} />
            );
          })}
        </tbody>
      </table>
    </>
  );
}

function FragmentRow({ p, s, lifecycle, open, onToggle }: { p: PlaybookDef; s: Strategy | undefined; lifecycle: string | undefined; open: boolean; onToggle: () => void }) {
  return (
    <>
      <tr>
        <td><button className="linklike" onClick={onToggle} aria-expanded={open}>{p.name}</button></td>
        <td>{CATALOG.families[p.family]}</td>
        <td>{HORIZON_TEXT[p.horizon]}</td>
        <td>{p.holding_days} 日{p.stop_loss_pct ? `，止损 ${p.stop_loss_pct}%` : ''}{p.trailing_stop_pct ? `，追踪 ${p.trailing_stop_pct}%` : ''}</td>
        <td><GradeBadge grade="untested" /> <span className="muted small">{lifecycle === 'specified' ? '已登记' : lifecycle ?? '未登记'}</span></td>
        <td>{p.requires.length ? <Badge tone="bad" title={p.requires.map((r) => REQ_TEXT[r] ?? r).join('；')}>缺数据</Badge> : <Badge tone="good">齐全</Badge>}</td>
        <td><Link to="/screen" className="small">去筛选</Link></td>
      </tr>
      {open && (
        <tr className="detail"><td colSpan={7}>
          <p>{p.rule}</p>
          <p className="muted small">来源：{p.source}。{p.requires.length ? `缺少：${p.requires.map((r) => REQ_TEXT[r] ?? r).join('；')}。实验会被记录为失败试验（data_not_available），不会编造结果。` : ''}</p>
          <div className="chips">{p.params.map((x) => <span key={x.key} className="chip">{x.label} {x.value}</span>)}<span className="chip">最多 {p.max_positions} 只</span></div>
          {s ? <Experiments strategy={s} /> : <MissingText>未登记</MissingText>}
        </td></tr>
      )}
    </>
  );
}

const MAIN_METRICS: Array<[string, string, string]> = [
  ['net_return', 'train', '净收益·训练'], ['net_return', 'validation', '净收益·验证'], ['net_return_cost_x2', 'validation', '2倍费用·验证'], ['net_return_cost_x3', 'validation', '3倍费用·验证'],
  ['excess_return_vs_csi300', 'validation', '超额(沪深300)·验证'], ['max_drawdown', 'overall', '最大回撤'], ['win_rate_gross', 'overall', '胜率(毛)'],
];
function Experiments({ strategy }: { strategy: Strategy }) {
  const ver = useQuery({ queryKey: ['extb', 'ver', strategy.strategy_id, strategy.strategy_version], queryFn: () => extB.queryStrategyVersion(strategy.strategy_id, strategy.strategy_version) });
  const ids = ver.data?.data.evaluation_ids ?? [];
  const evs = useQueries({ queries: ids.map((id) => ({ queryKey: ['extb', 'eval', id], queryFn: ({ signal }: { signal: AbortSignal }) => ops.getEvaluation(id, { signal }) })) });
  if (ver.isLoading) return <Loading />;
  if (ver.isError) return <ErrorState error={ver.error} what="实验列表" />;
  if (ids.length === 0) return <EmptyState>还没有实验。运行：<code>uv run python -m pulse.workers.research ext-b-run --ids PB:{(strategy.specification as { playbook?: { id?: string } }).playbook?.id}</code></EmptyState>;
  return (
    <div className="exps">
      {evs.map((q, i) => q.data ? <EvalCard key={ids[i]} ev={q.data.data} /> : <Loading key={ids[i]} />)}
    </div>
  );
}

export function EvalCard({ ev }: { ev: Evaluation }) {
  const get = (name: string, slice: string): string | null | undefined => ev.metrics.find((m) => m.metric_name === name && m.slice_key === slice)?.value;
  const trades = ev.metrics.find((m) => m.metric_name === 'trade_count')?.value;
  const failed = ev.run_state === 'failed';
  return (
    <div className="exp">
      <div><b>{ev.run_purpose === 'screening' ? '筛选实验' : ev.run_purpose}</b> · {ev.run_state === 'completed' ? '已完成' : failed ? '失败' : '排队/运行中'} <span className="muted small">{ev.context_hash.slice(0, 8)}</span>
        {failed && <Badge tone="bad" title="失败的试验也计入试验总数">{ev.error_code ?? '失败'}</Badge>}</div>
      {ev.run_state === 'completed' && (
        <>
          <table className="table dense"><tbody>
            <tr>{MAIN_METRICS.map(([, , l]) => <th key={l}>{l}</th>)}<th>成交笔数</th></tr>
            <tr>{MAIN_METRICS.map(([n, s, l]) => <td key={l} className="num">{metricPct(get(n, s))}</td>)}<td className="num">{trades == null ? <MissingText /> : Number(trades)}</td></tr>
          </tbody></table>
          <p className="muted small">价格为前复权研究序列、成交额为估算，质量标记为「部分」；净收益已含用户费用口径；盲测时段未被读取。数值不代表已验证的盈利能力。</p>
        </>
      )}
    </div>
  );
}

// ---- tactic replay ------------------------------------------------------------------------------------------------------
function Tactics() {
  const toast = useToast();
  const ctx = useQuery({ queryKey: ['extb', 'context'], queryFn: ({ signal }) => extB.context(signal), staleTime: 60_000 });
  const [instId, setInstId] = useState('');
  const [codes, setCodes] = useState('600001,600002');
  const [lots, setLots] = useState<Record<string, { qty: string; cost: string }>>({});
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [cash, setCash] = useState('200000');
  const [seed, setSeed] = useState('1');
  const [expId, setExpId] = useState<string | null>(null);
  const replay = ctx.data?.data.replay;
  const inst = replay?.instances.find((i) => i.instance_id === instId) ?? replay?.instances[0];
  const labels = useQuery({ queryKey: ['extb', 'codes', codes], queryFn: () => extB.instrumentLabels({ codes: codes.split(/[,\s]+/).filter(Boolean) }), enabled: codes.trim().length >= 6 });
  const found = labels.data?.data ?? [];
  const submit = useMutation({
    mutationFn: () => {
      if (!replay?.evaluation_context || !inst) throw new Error('还没有封存的评价上下文：先运行一次研究 worker');
      const spec: ReplaySpec = {
        instrument_ids: found.map((l) => l.instrument_id), window: { start: dayInstant(start), end: dayInstant(end) }, cash_cny: cash,
        opening_lots: found.filter((l) => Number(lots[l.code]?.qty) > 0).map((l) => ({ instrument_id: l.instrument_id, quantity: Number(lots[l.code]?.qty), eligible_on: '2020-01-01', cost_cny: lots[l.code]?.cost || '0' })),
        execution_policy_version: 'daily-bar-ohlc-path-v1', cost_policy_version: replay.cost_policy_versions[0] ?? replay.evaluation_context.cost_policy_version,
        benchmark_policy_version: 'same-inventory-hold-v1', training_mode: 'frozen', random_seed: Number(seed) || 1,
      };
      return extB.scheduleTacticReplay({ instance_id: inst.instance_id, hypothesis_id: inst.hypothesis_id, hypothesis_version: inst.hypothesis_version, evaluation_context_id: replay.evaluation_context.evaluation_context_id, replay_spec: spec });
    },
    onSuccess: (r) => { setExpId(r.data.receipt.resource_id); toast.push('good', '回放已排队'); },
    onError: (e) => toast.push('bad', `提交失败：${explain(e)}`),
  });
  if (ctx.isLoading) return <Loading />;
  if (ctx.isError) return <ErrorState error={ctx.error} what="回放上下文" onRetry={() => ctx.refetch()} />;
  if (!replay?.evaluation_context) return <EmptyState>还没有封存的评价上下文。先运行一次研究实验（<code>ext-b-run</code>），之后这里才能选择回放区间。</EmptyState>;
  const c = replay.evaluation_context;
  return (
    <>
      <p className="muted small">回放区间必须落在封存上下文的训练/验证时段内（{c.train_start.slice(0, 10)} ~ {c.validation_end.slice(0, 10)}），盲测时段不会混入。结果与「同初始资产、同底仓、同期末价格的持有不动」账户比较；期初浮盈不计为平台新收益。</p>
      <div className="form">
        <label>战术<select value={inst?.instance_id ?? ''} onChange={(e) => setInstId(e.target.value)}>{replay.instances.map((i) => <option key={i.instance_id} value={i.instance_id}>{i.display_name}{i.tactic.kind === 'intraday_t' ? ` ±${String(i.parameters.up_pct)}%/${String(i.parameters.rebuy_pct)}%` : ''}</option>)}</select></label>
        <label>股票代码（逗号分隔）<input value={codes} onChange={(e) => setCodes(e.target.value)} /></label>
        <label>起始日<input type="date" value={start} min={c.train_start.slice(0, 10)} max={c.validation_end.slice(0, 10)} onChange={(e) => setStart(e.target.value)} /></label>
        <label>结束日（不含）<input type="date" value={end} min={c.train_start.slice(0, 10)} max={c.validation_end.slice(0, 10)} onChange={(e) => setEnd(e.target.value)} /></label>
        <label>期初现金（元）<input value={cash} onChange={(e) => setCash(e.target.value)} inputMode="decimal" /></label>
        <label>随机种子<input value={seed} onChange={(e) => setSeed(e.target.value)} inputMode="numeric" /></label>
      </div>
      {found.length > 0 && (
        <table className="table dense"><thead><tr><th>代码</th><th>名称</th><th>底仓股数</th><th>底仓成本合计（元）</th></tr></thead><tbody>
          {found.map((l) => (
            <tr key={l.instrument_id}><td>{l.code}</td><td>{l.name}</td>
              <td><input aria-label={`${l.code}底仓股数`} inputMode="numeric" value={lots[l.code]?.qty ?? ''} onChange={(e) => setLots({ ...lots, [l.code]: { qty: e.target.value, cost: lots[l.code]?.cost ?? '' } })} /></td>
              <td><input aria-label={`${l.code}底仓成本`} inputMode="decimal" value={lots[l.code]?.cost ?? ''} onChange={(e) => setLots({ ...lots, [l.code]: { qty: lots[l.code]?.qty ?? '', cost: e.target.value } })} /></td></tr>
          ))}</tbody></table>
      )}
      <button className="btn btn-on" disabled={submit.isPending || !start || !end || found.length === 0} onClick={() => submit.mutate()}>提交回放</button>
      {expId && <ReplayResult experimentId={expId} />}
    </>
  );
}

const SLICE_TEXT = (s: string): string => (s === 'replay' ? '本方案' : s.startsWith('variant:') ? `卖法变体·${CATALOG.exit_variants.find((v) => `variant:${v.key}` === s)?.label ?? s.slice(8)}` : s.startsWith('param:') ? `参数 ${s.slice(6)}` : s === 'path_ohlc' ? '路径假设 开→高→低→收' : s === 'path_olhc' ? '路径假设 开→低→高→收' : s === 'path_spread' ? '路径敏感性' : s);
function ReplayResult({ experimentId }: { experimentId: string }) {
  const q = useQuery({
    queryKey: ['extb', 'replay', experimentId], queryFn: ({ signal }) => ops.getEvaluation(experimentId, { signal }),
    refetchInterval: (query) => (['completed', 'failed', 'cancelled'].includes(query.state.data?.data.run_state ?? '') ? false : 3000),
  });
  if (q.isLoading) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} what="回放结果" />;
  const ev = q.data!.data;
  if (ev.run_state === 'failed') return <div className="state state-error" role="alert">回放失败：{ev.error_code}（失败的试验同样被记录）</div>;
  if (ev.run_state !== 'completed') return <Loading label="回放排队/运行中…（由后台任务执行：uv run python -m pulse.workers.research tactic-run）" />;
  const slices = [...new Set(ev.metrics.map((m) => m.slice_key))];
  const val = (slice: string, name: string): string | null | undefined => ev.metrics.find((m) => m.slice_key === slice && m.metric_name === name)?.value;
  const money = (v: string | null | undefined): string => (v == null ? '缺失' : Number(v).toFixed(2));
  return (
    <section aria-label="回放结果">
      <h3>相对持有不动的增量（扣费后，全部方案都保存）</h3>
      <table className="table dense">
        <thead><tr><th>方案</th><th>增量(元)</th><th>持有净值</th><th>方案净值</th><th>费用</th><th>现金差</th><th>库存价值差</th><th>少持股数</th><th>腿完成/尝试</th></tr></thead>
        <tbody>
          {slices.filter((s) => s !== 'path_spread').map((s) => (
            <tr key={s}><td>{SLICE_TEXT(s)}</td><td className="num">{money(val(s, 'incremental_pnl_vs_hold'))}</td><td className="num">{money(val(s, 'baseline_nav'))}</td><td className="num">{money(val(s, 'active_nav'))}</td>
              <td className="num">{money(val(s, 'fees_active'))}</td><td className="num">{money(val(s, 'cash_delta'))}</td><td className="num">{money(val(s, 'inventory_value_delta'))}</td>
              <td className="num">{val(s, 'inventory_shortfall_shares') ?? '缺失'}</td><td className="num">{Number(val(s, 'legs_completed') ?? 0)}/{Number(val(s, 'legs_attempted') ?? 0)}</td></tr>
          ))}
        </tbody>
      </table>
      {slices.includes('path_spread') && <p className="small">路径敏感性（两种日内路径假设下增量之差）：<b>{money(val('path_spread', 'path_sensitivity'))}</b> 元。日线无法证明最高/最低价谁先出现，T 的两条腿只在声明的路径下才可能同日成交。</p>}
      <p className="muted small">未成交的腿保持未成交；少持的股数按期末价格计入增量。这是研究回放，不是收益承诺。</p>
    </section>
  );
}
