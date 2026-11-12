/**
 * Evidence / diagnostics drawer: the only place implementation details (request ids,
 * snapshot refs, policy versions, raw refs) are shown. Pages stay in plain language.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import type { ResponseMeta } from '../api/types';
import { fmtTime } from '../lib/format';

export interface DiagnosticsPayload {
  title: string;
  meta?: ResponseMeta | null;
  rows?: Array<[string, ReactNode]>;
}

interface Api {
  open: (p: DiagnosticsPayload) => void;
}
const Ctx = createContext<Api>({ open: () => undefined });
export const useDiagnostics = (): Api => useContext(Ctx);

export function DiagnosticsProvider({ children }: { children: ReactNode }) {
  const [payload, setPayload] = useState<DiagnosticsPayload | null>(null);
  const open = useCallback((p: DiagnosticsPayload) => setPayload(p), []);
  const api = useMemo(() => ({ open }), [open]);
  useEffect(() => {
    if (!payload) return undefined;
    const onKey = (e: KeyboardEvent): void => { if (e.key === 'Escape') setPayload(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [payload]);
  return (
    <Ctx.Provider value={api}>
      {children}
      {payload && <DiagnosticsDrawer payload={payload} onClose={() => setPayload(null)} />}
    </Ctx.Provider>
  );
}

function DiagnosticsDrawer({ payload, onClose }: { payload: DiagnosticsPayload; onClose: () => void }) {
  const m = payload.meta;
  return (
    <div className="drawer-wrap">
      <button className="drawer-scrim" aria-label="关闭详情" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`${payload.title} 的证据与诊断`}>
        <header className="drawer-head">
          <div>
            <div className="eyebrow">证据与诊断</div>
            <h2>{payload.title}</h2>
          </div>
          <button className="btn btn-ghost" onClick={onClose}>关闭</button>
        </header>
        <div className="drawer-body">
          {m && (
            <>
              <dl className="kv">
                <dt>请求编号</dt><dd className="mono">{m.request_id}</dd>
                <dt>追踪编号</dt><dd className="mono">{m.trace_id ?? '无'}</dd>
                <dt>合同版本</dt><dd>{m.contract_version}</dd>
                <dt>发布版本</dt><dd className="mono">{m.release_id ?? '无（非分析类响应）'}</dd>
                <dt>数据截点</dt><dd>{fmtTime(m.decision_as_of)}</dd>
                <dt>响应生成</dt><dd>{fmtTime(m.generated_at)}</dd>
                <dt>可得性政策</dt><dd>{m.availability_policy_version ?? '无'}</dd>
                <dt>数据质量</dt><dd>{m.quality_status}</dd>
                <dt>新鲜度</dt><dd>{m.freshness_state}</dd>
                <dt>覆盖</dt>
                <dd>
                  预期 {m.coverage.expected_count ?? '未知'} / 实际 {m.coverage.observed_count} / 缺失 {m.coverage.missing_count ?? '未知'}（政策 {m.coverage.policy_version}）
                  {m.coverage.unverifiable_reasons.length > 0 && <ul>{m.coverage.unverifiable_reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
                </dd>
              </dl>
              {m.warnings.length > 0 && (
                <>
                  <h3>提示</h3>
                  <ul className="plain">
                    {m.warnings.map((w) => (
                      <li key={`${w.code}${w.affected_component ?? ''}`}><b className="mono">{w.code}</b> {w.message}{w.affected_component ? `（影响：${w.affected_component}）` : ''}</li>
                    ))}
                  </ul>
                </>
              )}
              <h3>输入快照</h3>
              {m.input_snapshot_refs.length === 0 ? <p className="muted">无</p> : (
                <ul className="plain">
                  {m.input_snapshot_refs.map((s) => (
                    <li key={s.snapshot_id}><span className="mono">{s.dataset_id}</span> v{s.manifest_version}<br /><span className="mono small">{s.snapshot_id}</span></li>
                  ))}
                </ul>
              )}
            </>
          )}
          {payload.rows && payload.rows.length > 0 && (
            <>
              <h3>对象详情</h3>
              <dl className="kv">
                {payload.rows.map(([k, v]) => (
                  <div key={k} className="kv-row"><dt>{k}</dt><dd className="mono-wrap">{v}</dd></div>
                ))}
              </dl>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

export function DetailsButton({ title, meta, rows }: DiagnosticsPayload) {
  const { open } = useDiagnostics();
  return <button className="link-btn" onClick={() => open({ title, meta, rows })}>证据与详情</button>;
}
