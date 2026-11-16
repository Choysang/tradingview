/**
 * Command palette (Ctrl+K / Cmd+K, triage #50): jump to a page or search stocks by code or name.
 * Stocks come from the board lane instrument-search read (code prefix / name contains, current labels); a failed search is
 * shown as a failure, never as "no result". Esc or a click outside closes it; focus returns to where it was.
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { boardOps, type InstrumentHit } from '../pages/bf/board/api';
import { matchPages, stockTarget, type PageItem } from '../pages/bf/board/palette';
import '../pages/bf/board/board.css';

type Entry = { kind: 'page'; key: string; page: PageItem } | { kind: 'stock'; key: string; hit: InstrumentHit };

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

export function CommandPalette({ open, onClose, pages, release }: { open: boolean; onClose: () => void; pages: readonly PageItem[]; release: string | null }) {
  const nav = useNavigate();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const [sel, setSel] = useState(0);
  const q = useDebounced(query.trim(), 180);
  const search = useQuery({
    queryKey: ['bf-board', 'instrument-search', q],
    queryFn: ({ signal }) => boardOps.instrumentSearch(q, signal),
    enabled: open && q.length > 0,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (!open) return undefined;
    const back = document.activeElement as HTMLElement | null;
    setQuery('');
    setSel(0);
    inputRef.current?.focus();
    return () => back?.focus?.();
  }, [open]);

  const entries = useMemo<Entry[]>(() => {
    const p: Entry[] = matchPages(pages, query).map((page) => ({ kind: 'page', key: `p:${page.path}`, page }));
    const s: Entry[] = (q.length > 0 && search.data ? search.data.data.items : []).map((hit) => ({ kind: 'stock', key: `s:${hit.instrument_id}`, hit }));
    return [...s, ...p];
  }, [pages, query, q, search.data]);
  useEffect(() => { setSel(0); }, [query, search.data]);

  if (!open) return null;
  const pick = (e: Entry | undefined): void => {
    if (!e) return;
    onClose();
    nav(e.kind === 'page' ? e.page.path : stockTarget(e.hit.instrument_id, release));
  };
  const onKey = (e: React.KeyboardEvent): void => {
    if (e.key === 'Escape') { e.preventDefault(); onClose(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setSel((i) => Math.min(i + 1, Math.max(entries.length - 1, 0))); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((i) => Math.max(i - 1, 0)); }
    else if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); pick(entries[sel]); }
  };
  const stocks = entries.filter((e) => e.kind === 'stock');
  const pageEntries = entries.filter((e) => e.kind === 'page');
  const row = (e: Entry): React.ReactNode => {
    const i = entries.indexOf(e);
    return (
      <li key={e.key} id={`${listId}-${i}`} role="option" aria-selected={i === sel} onMouseEnter={() => setSel(i)} onMouseDown={(ev) => { ev.preventDefault(); pick(e); }}>
        {e.kind === 'page'
          ? <><span>{e.page.label}</span><span className="muted small mono">{e.page.path}</span></>
          : <><span>{e.hit.name ?? '（无名称）'}{e.hit.is_risk_warning ? ' · 风险警示' : ''}</span><span className="muted small mono">{e.hit.code ?? e.hit.instrument_id.slice(0, 8)}</span></>}
      </li>
    );
  };
  return (
    <div className="bd-palette-backdrop" onMouseDown={onClose}>
      <div className="bd-palette" role="dialog" aria-modal="true" aria-label="命令面板" onMouseDown={(e) => e.stopPropagation()} onKeyDown={onKey}>
        <input ref={inputRef} role="combobox" aria-expanded="true" aria-controls={listId} aria-activedescendant={entries.length ? `${listId}-${sel}` : undefined}
          aria-label="搜索股票或页面" placeholder="输入股票代码或名称，或页面名称" value={query} onChange={(e) => setQuery(e.target.value)} />
        <ul id={listId} role="listbox" aria-label="结果">
          {stocks.length > 0 && <li className="bd-palette-group" role="presentation">股票</li>}
          {stocks.map(row)}
          {pageEntries.length > 0 && <li className="bd-palette-group" role="presentation">页面</li>}
          {pageEntries.map(row)}
          {q.length > 0 && search.isLoading && <li className="muted small" role="presentation">搜索股票中…</li>}
          {q.length > 0 && search.error && <li className="muted small" role="presentation">股票搜索失败，稍后再试（页面仍可跳转）。</li>}
          {entries.length === 0 && !search.isLoading && !search.error && <li className="muted small" role="presentation">没有匹配项。</li>}
        </ul>
        <div className="bd-palette-foot">↑↓ 选择 · Enter 打开 · Esc 关闭 · 在问句选股页按 / 聚焦问句栏</div>
      </div>
    </div>
  );
}
