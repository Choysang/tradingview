/** Seat profile: conditional post-disclosure statistics from the profile artifact. Sample size and limits come first. */
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Badge } from '../../../components/Badges';
import { EmptyState, Loading } from '../../../components/States';
import { extGet } from './api';
import { ArtifactView, ExtError, Strip } from './common';
import { fmtMetricValue } from './fmt';
import { isoSeconds, windowLastDays } from './time';
import type { ArtifactData, SeatProfileBody, SeatProfileData } from './types';

export interface SeatPick { seat_id: string; seat_version: number; name: string }
export const DEFAULT_SEAT_POLICY = 'seat-profile-v1';

function ProfileBody({ data, sampleCount, limitations }: { data: ArtifactData; sampleCount: number; limitations: string[] }) {
  const body = data.body as SeatProfileBody;
  const metrics = body.metrics ?? [];
  const lims = Array.from(new Set([...(limitations ?? []), ...(body.limitations ?? [])]));
  return (
    <>
      <div className="callout" role="note">
        <b>样本数 {body.sample_count ?? sampleCount}</b>
        {(body.sample_count ?? sampleCount) < 30 && <span>（样本很少，统计不稳定）</span>}
        <span> · 统计政策 {body.policy_version ?? '未标明'}</span>
      </div>
      <h4>使用限制</h4>
      {lims.length === 0 ? <p className="small muted">服务端没有给出使用限制（不代表没有限制）。</p> : <ul className="plain">{lims.map((l) => <li key={l} className="flag flag-warn">{l}</li>)}</ul>}
      {metrics.length === 0 ? <EmptyState>没有可展示的统计指标。</EmptyState> : (
        <div className="table-wrap">
          <table className="table">
            <caption className="muted small">披露后的条件统计，仅描述历史样本，不是预测</caption>
            <thead><tr><th scope="col">指标</th><th scope="col">取值</th><th scope="col">样本数</th><th scope="col">说明</th></tr></thead>
            <tbody>
              {metrics.map((m) => (
                <tr key={`${m.name}-${m.definition_version}`}>
                  <td>{m.name}<div className="muted small">定义 {m.definition_version}</div></td>
                  <td className="num">{m.value === null ? <Badge tone="warn">样本不足</Badge> : fmtMetricValue(m.value, m.unit)}</td>
                  <td className="num">{m.sample_count}</td>
                  <td className="small">{m.missing_reason ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export function SeatProfile({ seat, onClose, nowMs }: { seat: SeatPick; onClose: () => void; nowMs: number }) {
  const [policy, setPolicy] = useState(DEFAULT_SEAT_POLICY);
  const [applied, setApplied] = useState(DEFAULT_SEAT_POLICY);
  const params = useMemo(() => ({ seat_id: seat.seat_id, seat_version: seat.seat_version, decision_as_of: isoSeconds(nowMs), window: windowLastDays(365, nowMs), policy_version: applied }), [seat, nowMs, applied]);
  const q = useQuery({ queryKey: ['extc', 'seat-profile', params], queryFn: ({ signal }) => extGet<SeatProfileData>('query-seat-profile', params, signal) });
  return (
    <section className="card" aria-label={`席位画像 ${seat.name}`}>
      <div className="item-head"><h2>{seat.name}</h2><Badge tone="good">已确认席位</Badge><span className="grow" /><button className="btn btn-small" onClick={onClose}>关闭画像</button></div>
      <p className="muted small">统计窗口：最近 365 天；只统计该席位在龙虎榜披露之后的表现，是条件统计，不构成预测。</p>
      <form className="filters" onSubmit={(e) => { e.preventDefault(); setApplied(policy.trim()); }}>
        <label className="select-label">统计政策版本<input value={policy} onChange={(e) => setPolicy(e.target.value)} /></label>
        <button className="btn btn-small">应用</button>
      </form>
      {q.isLoading ? <Loading /> : q.error ? <ExtError error={q.error} onRetry={() => { void q.refetch(); }} what="席位画像" notReadyText="画像尚未生成" /> : q.data ? (
        <>
          <Strip meta={q.data.meta} title="席位画像" />
          <ArtifactView artifact={q.data.data.profile} render={(a) => <ProfileBody data={a} sampleCount={q.data.data.sample_count} limitations={q.data.data.limitations} />} />
        </>
      ) : null}
    </section>
  );
}
