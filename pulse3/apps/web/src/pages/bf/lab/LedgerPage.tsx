/**
 * 历史实验台账 (triage #177 #178): the old lab's 92 experiments as READ-ONLY history, plus "why was this old tool retired".
 * These are not pulse3 experiments (those are registered before they run, see the experiment archive); they are what was learned before.
 * Verdict, why, and when to try again are the first things on every card. ``?entry=E003`` opens one entry; ``?tool=trad_battery`` filters the retired-tool table.
 */
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useDeferredValue, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Badge } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import type { EntryBrief, LedgerEntry, RetiredFamily, Verdict } from './api';
import { bfLab, VERDICTS } from './api';
import { VERDICT_TEXT, VERDICT_TONE } from './format';
import './lab.css';

const PAGE = 100;
const entryLink = (id: string) => ({ search: `?entry=${encodeURIComponent(id)}` });

export function VerdictBadge({ verdict }: { verdict: Verdict | null }) {
  return verdict == null ? <Badge>未导入</Badge> : <Badge tone={VERDICT_TONE[verdict]} title={`verdict = ${verdict}`}>{VERDICT_TEXT[verdict]}</Badge>;
}

export function EntryLink({ e }: { e: EntryBrief }) {
  return <Link to={entryLink(e.id)} title={e.name ?? undefined} className="entry-link">{e.id}{e.verdict && <> · {VERDICT_TEXT[e.verdict]}</>}</Link>;
}

export function LedgerPage() {
  const [params] = useSearchParams();
  const entryId = params.get('entry');
  const [verdict, setVerdict] = useState<Verdict | ''>('');
  const [track, setTrack] = useState('');
  const [text, setText] = useState('');
  const q = useDeferredValue(text);
  const filter = { verdict, track, q };
  const list = useInfiniteQuery({
    queryKey: ['bf-lab', 'ledger', filter], retry: false, initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => bfLab.ledger(filter, pageParam, PAGE, signal),
    getNextPageParam: (p) => p.data.next_offset,
  });
  const first = list.data?.pages[0]?.data;
  const entries = list.data?.pages.flatMap((p) => p.data.entries) ?? [];
  return (
    <section className="bfl ledger">
      <h2>历史实验台账</h2>
      <p className="muted small">旧实验室留下的 {first?.counts?.total ?? 92} 条实验记录，只读。失败的方法也在里面：它失败时的市场环境，以及什么情况下值得再试。它们不是 pulse3 的实验（pulse3 的试验先登记、后运行，见实验档案）。</p>
      {entryId && <EntryDetail id={entryId} />}
      {list.isLoading && <Loading />}
      {list.isError && <ErrorState error={list.error} what="实验台账" onRetry={() => list.refetch()} />}
      {first && first.ledger == null && <EmptyState>台账还没有导入。运行 <code>uv run python -m pulse.workers.lab import-ledger</code> 后刷新。</EmptyState>}
      {first?.ledger && (
        <>
          <div className="counts">
            <button className={`btn btn-small ${verdict === '' ? 'btn-on' : ''}`} onClick={() => setVerdict('')}>全部 {first.counts?.total}</button>
            {VERDICTS.map((v) => (
              <button key={v} className={`btn btn-small ${verdict === v ? 'btn-on' : ''}`} aria-pressed={verdict === v} onClick={() => setVerdict(v)}>
                {VERDICT_TEXT[v]} {first.counts?.by_verdict[v]}
              </button>
            ))}
          </div>
          <div className="filters">
            <label className="select-label">方向
              <select value={track} onChange={(e) => setTrack(e.target.value)}>
                <option value="">全部</option>
                {Object.entries(first.counts?.by_track ?? {}).map(([t, n]) => <option key={t} value={t}>{t}（{n}）</option>)}
              </select>
            </label>
            <label className="select-label grow">搜索（名称、原因、结果、产物…）<input value={text} onChange={(e) => setText(e.target.value)} placeholder="例如 动量、E036、curve_gate" /></label>
          </div>
          <p className="muted small">版本 {first.ledger.label}，sha256 {first.ledger.ledger_sha256.slice(0, 12)}…，匹配 {first.matched} 条。</p>
          {entries.length === 0 && <EmptyState>没有符合条件的实验。</EmptyState>}
          <ul className="entries plain">{entries.map((e) => <li key={e.id}><EntryCard e={e} open={e.id === entryId} /></li>)}</ul>
          {list.hasNextPage && <button className="btn" disabled={list.isFetchingNextPage} onClick={() => list.fetchNextPage()}>{list.isFetchingNextPage ? '加载中…' : '加载更多'}</button>}
        </>
      )}
      <RetiredTools />
    </section>
  );
}

export function EntryCard({ e, open = false }: { e: LedgerEntry; open?: boolean }) {
  return (
    <details className={`entry verdict-${e.verdict}`} open={open}>
      <summary>
        <span className="entry-head"><b>{e.id}</b> <span className="muted small">{e.date} · 第 {e.round} 轮 · {e.track}</span> <VerdictBadge verdict={e.verdict} /></span>
        <span className="entry-name">{e.name}</span>
      </summary>
      <div className="why-grid">
        <div className="why"><h4>为什么</h4><p>{e.why}</p></div>
        <div className="retry"><h4>何时再试</h4><p>{e.reactivate_when}</p></div>
      </div>
      <dl className="entry-facts">
        <dt>假设</dt><dd>{e.hypothesis}</dd>
        <dt>做法</dt><dd>{e.config}</dd>
        <dt>结果</dt>
        <dd>{Object.keys(e.results).length === 0 ? <span className="missing">没有记录</span> : <ul className="plain">{Object.entries(e.results).map(([k, v]) => <li key={k}><b>{k}</b>：{v}</li>)}</ul>}</dd>
        <dt>市场环境</dt><dd>{e.regime}</dd>
        {e.caveat && <><dt>已知局限</dt><dd>{e.caveat}</dd></>}
        {e.supersedes.length > 0 && <><dt>取代了</dt><dd>{e.supersedes.map((i) => <Link key={i} to={entryLink(i)} className="entry-link">{i}</Link>)}</dd></>}
        {e.superseded_by.length > 0 && <><dt>被取代于</dt><dd>{e.superseded_by.map((i) => <Link key={i} to={entryLink(i)} className="entry-link">{i}</Link>)}</dd></>}
        {e.retired_families.length > 0 && <><dt>相关退役工具</dt><dd><Link to={{ search: `?tool=${encodeURIComponent(e.retired_families[0] ?? "")}` }} className="entry-link">{e.retired_families.join('、')}</Link></dd></>}
        <dt>产物</dt><dd>{e.artifacts.length === 0 ? <span className="missing">没有</span> : <ul className="plain">{e.artifacts.map((a) => <li key={a}><code>{a}</code></li>)}</ul>}</dd>
      </dl>
    </details>
  );
}

function EntryDetail({ id }: { id: string }) {
  const q = useQuery({ queryKey: ['bf-lab', 'entry', id], queryFn: ({ signal }) => bfLab.ledgerEntry(id, signal), retry: false });
  if (q.isLoading) return <Loading label={`读取 ${id}…`} />;
  if (q.isError) return <ErrorState error={q.error} what={`实验 ${id}`} />;
  const d = q.data!.data;
  return (
    <aside className="entry-detail" aria-label={`实验 ${id}`}>
      <EntryCard e={d.entry} open />
      {d.superseded_by_entries.length > 0 && <p className="small">被取代于：{d.superseded_by_entries.map((b) => <EntryLink key={b.id} e={b} />)}</p>}
      {d.supersedes_entries.length > 0 && <p className="small">取代了：{d.supersedes_entries.map((b) => <EntryLink key={b.id} e={b} />)}</p>}
      {d.retired_families.map((f) => (
        <p key={f.family_id} className="small retired-note"><Badge tone="bad">已退役</Badge> 这条记录是「{f.name}」被退役的依据：{f.reason_zh}</p>
      ))}
    </aside>
  );
}

/** "This tool was retired because …": family -> ledger entries -> reason. Filter with ?tool=<name>. */
export function RetiredTools() {
  const [params, setParams] = useSearchParams();
  const tool = params.get('tool') ?? '';
  const q = useQuery({ queryKey: ['bf-lab', 'retired', tool], queryFn: ({ signal }) => bfLab.retiredTools(tool ? { tool } : {}, signal), retry: false });
  return (
    <section className="retired" aria-label="已退役的研究工具">
      <h3>已退役的研究工具</h3>
      <p className="muted small">这些工具的代码没有移植；它们的结论保留在上面的台账里。</p>
      <label className="select-label">按工具名查找
        <input value={tool} onChange={(e) => { const n = new URLSearchParams(params); if (e.target.value) n.set('tool', e.target.value); else n.delete('tool'); setParams(n, { replace: true }); }} placeholder="例如 trad_battery、rl_fqi" />
      </label>
      {q.isLoading && <Loading />}
      {q.isError && <ErrorState error={q.error} what="退役工具" onRetry={() => q.refetch()} />}
      {q.isSuccess && q.data.data.families.length === 0 && <EmptyState>没有叫这个名字的退役工具。</EmptyState>}
      {q.isSuccess && q.data.data.families.length > 0 && (
        <div className="scroll">
          <table className="table dense">
            <thead><tr><th>工具族</th><th>工具</th><th>台账</th><th>退役原因</th></tr></thead>
            <tbody>{q.data.data.families.map((f) => <FamilyRow key={f.family_id} f={f} />)}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function FamilyRow({ f }: { f: RetiredFamily }) {
  return (
    <tr>
      <td>{f.name}{f.old_rows.length > 0 && <div className="muted small">旧功能行 {f.old_rows.map((r) => `#${r}`).join('、')}</div>}</td>
      <td>{f.tools.map((t) => <code key={t} className="tool">{t}</code>)}</td>
      <td>{f.entries.map((b) => <div key={b.id}><EntryLink e={b} /></div>)}{f.also_see.length > 0 && <div className="muted small">另见 {f.also_see.join('、')}</div>}</td>
      <td>{f.reason_zh}</td>
    </tr>
  );
}
