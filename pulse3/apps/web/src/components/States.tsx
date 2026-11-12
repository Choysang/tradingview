import type { ReactNode } from 'react';
import { ApiError } from '../api/client';

export function Loading({ label = '读取中…' }: { label?: string }) {
  return <div className="state state-loading" role="status"><span className="spinner" aria-hidden="true" />{label}</div>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="state state-empty">{children}</div>;
}

export function ErrorState({ error, onRetry, what }: { error: unknown; onRetry?: () => void; what?: string }) {
  const e = error instanceof ApiError ? error : null;
  return (
    <div className="state state-error" role="alert">
      <div><b>{what ? `${what}读取失败` : '读取失败'}</b>{e ? `（${e.code}）` : ''}</div>
      <div className="small">{error instanceof Error ? error.message : '未知错误'}</div>
      {onRetry && <button className="btn btn-small" onClick={onRetry}>重试</button>}
    </div>
  );
}

export function SectionTitle({ title, hint, right }: { title: string; hint?: ReactNode; right?: ReactNode }) {
  return (
    <div className="section-title">
      <div>
        <h2>{title}</h2>
        {hint && <p className="muted small">{hint}</p>}
      </div>
      {right}
    </div>
  );
}
