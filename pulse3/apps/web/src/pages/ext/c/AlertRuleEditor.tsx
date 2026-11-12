/** Alert rule list + create/enable/disable (saveAlertRule). Nothing is ever sent from the browser: delivery is server-side. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { Badge } from '../../../components/Badges';
import { EmptyState, Loading, SectionTitle } from '../../../components/States';
import { useToast } from '../../../components/Toast';
import { fmtTime } from '../../../lib/format';
import { ALERT_POLICY_VERSION, SCOPE_KIND_LABEL, buildRuleBody, validateDraft, withEnabled, type RuleDraft, type ScopeKind } from './alertRule';
import { explainError, extGet, extPost } from './api';
import { ExtError } from './common';
import type { AlertDestination, AlertRuleEntry, AlertRulesData } from './types';

const RULES_KEY = ['extc', 'alert-rules'];
const CHANNEL = { in_app: '站内', webhook: 'Webhook' } as const;

function scopeText(r: AlertRuleEntry): string {
  const k = r.body.scope.scope_kind as ScopeKind;
  return `${SCOPE_KIND_LABEL[k] ?? r.body.scope.scope_kind}：${r.body.scope.scope_key}`;
}

function DestBadges({ ids, dests }: { ids: string[]; dests: AlertDestination[] }) {
  return (
    <span className="chips">
      {ids.map((id) => {
        const d = dests.find((x) => x.destination_id === id);
        return <Badge key={id} tone={d?.enabled ? 'neutral' : 'warn'} title={d ? `${d.label}（${d.endpoint === 'configured' ? '已配置' : '未配置地址'}）` : id}>{d ? CHANNEL[d.channel] : '未知去向'}</Badge>;
      })}
    </span>
  );
}

export function AlertRuleEditor() {
  const q = useQuery({ queryKey: RULES_KEY, queryFn: ({ signal }) => extGet<AlertRulesData>('c/alert-rules', undefined, signal) });
  const qc = useQueryClient();
  const toast = useToast();
  const save = useMutation({
    mutationFn: (v: { ruleId: string; expected: number; body: ReturnType<typeof buildRuleBody>; key: string }) =>
      extPost('save-alert-rule', { rule_id: v.ruleId, expected_rule_version: v.expected, body: v.body }, v.key),
    onSuccess: () => { toast.push('good', '告警规则已保存'); void qc.invalidateQueries({ queryKey: RULES_KEY }); },
    onError: (e) => { toast.push('bad', `保存失败：${explainError(e)}`); void qc.invalidateQueries({ queryKey: RULES_KEY }); },
  });

  // One draft = one rule_id + one idempotency key, so a double click cannot create two rules.
  const draftIds = useRef({ rule: crypto.randomUUID(), key: crypto.randomUUID() });
  const [draft, setDraft] = useState<RuleDraft>({ scopeKind: 'news_tier', scopeKey: 'P0', destinationIds: [], enabled: true });
  const [formErr, setFormErr] = useState<string | null>(null);

  if (q.isLoading) return <Loading />;
  if (q.error) return <ExtError error={q.error} onRetry={() => { void q.refetch(); }} what="告警规则" />;
  const data = q.data?.data;
  if (!data) return null;
  const dests = data.destinations;

  const create = (): void => {
    const err = validateDraft(draft);
    setFormErr(err);
    if (err) return;
    save.mutate({ ruleId: draftIds.current.rule, expected: 0, body: buildRuleBody(draft), key: draftIds.current.key }, {
      onSuccess: () => { draftIds.current = { rule: crypto.randomUUID(), key: crypto.randomUUID() }; },
    });
  };
  const toggle = (r: AlertRuleEntry): void => save.mutate({ ruleId: r.rule_id, expected: r.rule_version, body: withEnabled(r.body, !r.body.enabled), key: crypto.randomUUID() });
  const toggleDest = (id: string): void => setDraft((d) => ({ ...d, destinationIds: d.destinationIds.includes(id) ? d.destinationIds.filter((x) => x !== id) : [...d.destinationIds, id] }));

  return (
    <section className="card" aria-label="告警规则">
      <SectionTitle title="告警规则" hint={`规则策略版本 ${ALERT_POLICY_VERSION}`} />
      <div className={data.webhook.configured ? 'notice notice-info' : 'notice notice-warn'} role="note">
        {data.webhook.configured
          ? `Webhook 推送由服务器发出（${data.webhook.kind ?? 'webhook'} @ ${data.webhook.host ?? '已配置'}）。`
          : 'Webhook 推送只有在服务器配置了推送地址后才会生效；当前未配置，只会产生站内告警。'}
        <span>浏览器不会向任何地址发送内容。</span>
      </div>
      {data.rules.length === 0 ? <EmptyState>还没有告警规则。</EmptyState> : (
        <div className="table-wrap">
          <table className="table">
            <caption className="muted small">已有规则</caption>
            <thead><tr><th scope="col">范围</th><th scope="col">通知去向</th><th scope="col">版本</th><th scope="col">更新</th><th scope="col">状态</th></tr></thead>
            <tbody>
              {data.rules.map((r) => (
                <tr key={r.rule_id}>
                  <td>{scopeText(r)}</td>
                  <td><DestBadges ids={r.body.destination_ids} dests={dests} /></td>
                  <td className="mono">v{r.rule_version}</td>
                  <td className="small muted">{fmtTime(r.updated_at)}</td>
                  <td>
                    <button className={`btn btn-small ${r.body.enabled ? 'btn-on' : ''}`} aria-pressed={r.body.enabled} disabled={save.isPending} onClick={() => toggle(r)} aria-label={`${scopeText(r)}：${r.body.enabled ? '点击停用' : '点击启用'}`}>
                      {r.body.enabled ? '已启用' : '已停用'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h3>新建规则</h3>
      <form className="form" onSubmit={(e) => { e.preventDefault(); create(); }} aria-label="新建告警规则">
        <label>范围类型
          <select value={draft.scopeKind} onChange={(e) => setDraft({ ...draft, scopeKind: e.target.value as ScopeKind, scopeKey: e.target.value === 'news_tier' ? 'P0' : '' })}>
            {(Object.keys(SCOPE_KIND_LABEL) as ScopeKind[]).map((k) => <option key={k} value={k}>{SCOPE_KIND_LABEL[k]}</option>)}
          </select>
        </label>
        {draft.scopeKind === 'news_tier' ? (
          <label>分级
            <select value={draft.scopeKey} onChange={(e) => setDraft({ ...draft, scopeKey: e.target.value })}>
              <option value="P0">P0 持仓负面</option><option value="P1">P1 持仓行业政策负面</option><option value="P2">P2 自选或正面</option>
            </select>
          </label>
        ) : (
          <label>{draft.scopeKind === 'instrument' ? '证券编号' : '行业编号'}
            <input value={draft.scopeKey} onChange={(e) => setDraft({ ...draft, scopeKey: e.target.value })} />
          </label>
        )}
        <fieldset className="chips" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="small muted">通知去向</legend>
          {dests.map((d) => (
            <label key={d.destination_id} className="check">
              <input type="checkbox" checked={draft.destinationIds.includes(d.destination_id)} onChange={() => toggleDest(d.destination_id)} />
              {CHANNEL[d.channel]} {d.label}{d.channel === 'webhook' && d.endpoint !== 'configured' ? '（服务器未配置地址）' : ''}{d.enabled ? '' : '（已停用）'}
            </label>
          ))}
        </fieldset>
        <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />创建后立即启用</label>
        <button className="btn btn-on" disabled={save.isPending}>{save.isPending ? '保存中…' : '保存规则'}</button>
      </form>
      {formErr && <p className="field-error" role="alert">{formErr}</p>}
    </section>
  );
}
