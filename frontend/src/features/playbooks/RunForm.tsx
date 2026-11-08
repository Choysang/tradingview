import { useState } from 'react';
import type { FormEvent } from 'react';
import { Play } from 'lucide-react';
import type { ExitRules, PlaybookEvaluation, PlaybookRunRequest, PlaybookSpec, SizingRule } from '../../core/contract';
import { ErrorNote, Seg } from '../../kit/primitives';

type ExitKey = 'stop_loss_pct' | 'take_profit_pct' | 'trailing_stop_pct';

const EXIT_FIELDS: [ExitKey, string, number, number][] = [
  ['stop_loss_pct', '止损 %', 0.5, 50],
  ['take_profit_pct', '止盈 %', 1, 300],
  ['trailing_stop_pct', '移动止损 %', 0.5, 50],
];

interface RunFormProps {
  spec: PlaybookSpec;
  disabled: boolean;
  onRun: (body: PlaybookRunRequest) => Promise<PlaybookEvaluation>;
  onResult: (evaluation: PlaybookEvaluation) => void;
}

/** Re-run one playbook with other parameters, exits and sizing. Empty exit fields switch that exit off. */
export default function RunForm({ spec, disabled, onRun, onResult }: RunFormProps) {
  const [params, setParams] = useState<Record<string, number>>(() => Object.fromEntries(spec.params.map(param => [param.key, param.value])));
  const [exits, setExits] = useState<ExitRules>(spec.exits);
  const [sizing, setSizing] = useState<SizingRule>(spec.sizing);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onResult(await onRun({ params, exits, sizing }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="run-form" onSubmit={submit}>
      {spec.params.length > 0 && (
        <fieldset>
          <legend>信号参数</legend>
          <div className="run-grid">
            {spec.params.map(param => (
              <label key={param.key} className="param">
                <span>{param.label}</span>
                <input type="number" className="num" min={param.min} max={param.max} step={param.step} value={params[param.key] ?? param.value} onChange={event => setParams(current => ({ ...current, [param.key]: Number(event.target.value) }))} />
              </label>
            ))}
          </div>
        </fieldset>
      )}
      <fieldset>
        <legend>退出规则</legend>
        <div className="run-grid">
          <label className="param">
            <span>持有天数</span>
            <input type="number" className="num" min={1} max={250} value={exits.holding_days} onChange={event => setExits(current => ({ ...current, holding_days: Number(event.target.value) }))} />
          </label>
          {EXIT_FIELDS.map(([key, label, min, max]) => (
            <label key={key} className="param">
              <span>{label}</span>
              <input type="number" className="num" min={min} max={max} step={0.5} placeholder="关" value={exits[key] ?? ''} onChange={event => setExits(current => ({ ...current, [key]: event.target.value === '' ? null : Number(event.target.value) }))} />
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend>仓位</legend>
        <div className="run-grid">
          <div className="param">
            <span>分配</span>
            <Seg<SizingRule['method']> label="仓位分配" value={sizing.method} onChange={method => setSizing(current => ({ ...current, method }))} options={[{ value: 'equal', label: '等权' }, { value: 'volatility', label: '波动率倒数' }]} />
          </div>
          <label className="param">
            <span>最多持仓</span>
            <input type="number" className="num" min={1} max={50} value={sizing.max_positions} onChange={event => setSizing(current => ({ ...current, max_positions: Number(event.target.value) }))} />
          </label>
          <label className="param">
            <span>每日新开</span>
            <input type="number" className="num" min={1} max={20} value={sizing.max_new_per_day} onChange={event => setSizing(current => ({ ...current, max_new_per_day: Number(event.target.value) }))} />
          </label>
        </div>
      </fieldset>
      <div className="run-actions">
        <button className="btn primary" type="submit" disabled={busy || disabled}>{busy ? <span className="spinner" /> : <Play size={14} />}运行</button>
        {disabled && <span className="faint">该战法依赖的历史数据不可得，无法回测。</span>}
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
    </form>
  );
}
