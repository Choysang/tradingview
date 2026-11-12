/** Shared pieces for the extension-C pages: error/empty states, paging hook, stock links, artifact viewer. */
import { useInfiniteQuery, useQuery, type InfiniteData } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { Envelope } from '../../../api/types';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { MetaStrip } from '../../../components/MetaStrip';
import { extGet, fetchArtifact, isNotReady, explainError, type Params } from './api';
import type { ArtifactData, ArtifactRef, PageToken, ResponseMeta } from './types';
import './ext-c.css';

/** Anchor for "now"; fixed for the life of the page so query keys stay stable. `refresh` moves it. */
export function useNowAnchor(): [number, () => void] {
  const [now, setNow] = useState(() => Date.now());
  return [now, () => setNow(Date.now())];
}

/** Typed error with Chinese text; RESULT_NOT_READY is an expected state, not an error. */
export function ExtError({ error, onRetry, what, notReadyText = '结果尚未生成' }: { error: unknown; onRetry?: () => void; what?: string; notReadyText?: string }) {
  if (isNotReady(error)) return <EmptyState>{notReadyText}</EmptyState>;
  const msg = explainError(error);
  return (
    <div className="state state-error" role="alert">
      <div><b>{what ? `${what}读取失败` : '读取失败'}</b></div>
      <div className="small">{msg}</div>
      {onRetry && <button className="btn btn-small" onClick={onRetry}>重试</button>}
    </div>
  );
}
export { ErrorState };

export function Strip({ meta, title }: { meta: ResponseMeta | undefined; title: string }) {
  return meta ? <MetaStrip meta={meta} title={title} /> : null;
}

/** All pages share one snapshot_hash; a change mid-way means the list moved under the reader. */
export function pageSnapshotsConsistent(pages: ReadonlyArray<{ page: PageToken }>): boolean {
  const first = pages[0]?.page.snapshot_hash;
  return pages.every((p) => p.page.snapshot_hash === first);
}

export interface PagedResult<D> {
  pages: Array<Envelope<D>>;
  isLoading: boolean;
  isFetchingNextPage: boolean;
  error: unknown;
  hasMore: boolean;
  loadMore: () => void;
  refetch: () => void;
  consistent: boolean;
}

/** Cursor paging over a GET extension op. The cursor goes back unchanged; the other params must stay the same. */
export function usePaged<D extends { page: PageToken }>(op: string, params: Params, limit = 50, enabled = true): PagedResult<D> {
  const q = useInfiniteQuery<Envelope<D>, Error, InfiniteData<Envelope<D>, string | null>, unknown[], string | null>({
    queryKey: ['extc', op, params, limit],
    initialPageParam: null,
    enabled,
    queryFn: ({ pageParam, signal }) => extGet<D>(op, { ...params, page: pageParam ? { limit, cursor: pageParam } : { limit } }, signal),
    getNextPageParam: (last) => last.data.page.next_cursor ?? undefined,
  });
  const pages = q.data?.pages ?? [];
  return {
    pages, isLoading: q.isLoading, isFetchingNextPage: q.isFetchingNextPage, error: q.error,
    hasMore: q.hasNextPage, loadMore: () => { void q.fetchNextPage(); }, refetch: () => { void q.refetch(); },
    consistent: pageSnapshotsConsistent(pages.map((p) => p.data)),
  };
}

export function LoadMore({ paged }: { paged: PagedResult<{ page: PageToken }> }) {
  return (
    <>
      {!paged.consistent && <div className="notice notice-warn" role="alert">分页期间数据快照发生了变化，列表可能不完整，请点击刷新重新读取。<button className="btn btn-small" onClick={paged.refetch}>刷新</button></div>}
      {paged.hasMore && <div className="pager"><button className="btn btn-small" disabled={paged.isFetchingNextPage} onClick={paged.loadMore}>{paged.isFetchingNextPage ? '读取中…' : '加载更多'}</button></div>}
    </>
  );
}

export function Pending({ loading, error, onRetry, what }: { loading: boolean; error: unknown; onRetry?: () => void; what?: string }) {
  if (loading) return <Loading />;
  if (error) return <ExtError error={error} onRetry={onRetry} what={what} />;
  return null;
}

export function StockLink({ id, code, name }: { id: string; code?: string | null; name?: string | null }) {
  const label = name ? name : `证券 ${id.slice(0, 8)}`;
  return <Link to={`/stock/${id}`} className="inst-name"><b>{label}</b>{code && <span className="muted small mono"> {code}</span>}</Link>;
}

function scalar(v: unknown): ReactNode {
  if (v === null || v === undefined) return <span className="missing">缺失</span>;
  if (typeof v === 'object') return <code className="small">{JSON.stringify(v)}</code>;
  return String(v);
}

/** Generic view of an artifact body: the first array of objects becomes a table, the rest stays as JSON. Shape-agnostic on purpose. */
export function ArtifactBody({ body }: { body: Record<string, unknown> }) {
  const arrKey = Object.keys(body).find((k) => Array.isArray(body[k]) && (body[k] as unknown[]).length > 0 && (body[k] as unknown[]).every((r) => typeof r === 'object' && r !== null));
  const rows = arrKey ? (body[arrKey] as Array<Record<string, unknown>>) : [];
  const cols = arrKey ? Array.from(new Set(rows.flatMap((r) => Object.keys(r)))) : [];
  const rest = Object.fromEntries(Object.entries(body).filter(([k]) => k !== arrKey));
  return (
    <div className="artifact-body">
      {arrKey && (
        <div className="table-wrap">
          <table className="table"><caption className="muted small">{arrKey}</caption>
            <thead><tr>{cols.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
            <tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c}>{scalar(r[c])}</td>)}</tr>)}</tbody>
          </table>
        </div>
      )}
      {Object.keys(rest).length > 0 && <pre className="json-pre">{JSON.stringify(rest, null, 2)}</pre>}
    </div>
  );
}

/** Loads one artifact through the support route and renders it with `render` (default: generic body view). */
export function useArtifact(ref: ArtifactRef | null | undefined, enabled = true) {
  return useQuery({
    queryKey: ['extc', 'artifact', ref?.ref ?? null],
    queryFn: ({ signal }) => fetchArtifact(ref as ArtifactRef, signal),
    enabled: enabled && !!ref,
    staleTime: Infinity, // content-addressed by ref
  });
}

export function ArtifactView({ artifact, render }: { artifact: ArtifactRef; render?: (a: ArtifactData) => ReactNode }) {
  const q = useArtifact(artifact);
  if (q.isLoading) return <Loading label="读取证据…" />;
  if (q.error) return <ExtError error={q.error} onRetry={() => { void q.refetch(); }} what="证据" />;
  if (!q.data) return null;
  return <>{render ? render(q.data.data) : <ArtifactBody body={q.data.data.body} />}</>;
}
