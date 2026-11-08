import type { ReactNode } from 'react';
import { Database } from 'lucide-react';
import type { DataProvenance, RegimeState } from '../../core/contract';
import { num, pct } from '../../core/format';
import { DIMENSIONS, REGIME_STATES, stateMeta } from './labels';
import type { Dimension } from './labels';
import './regime.css';

function evidence(state: RegimeState, dimension: Dimension): string {
  switch (dimension) {
    case 'trend':
      return state.breadth_ma20 == null ? '站上 MA20 占比 —' : `站上 MA20 占比 ${num(state.breadth_ma20, 0)}%`;
    case 'liquidity':
      return state.amount_ratio == null ? '成交 20/120 日 —' : `成交 20/120 日 ${num(state.amount_ratio, 2)}×`;
    case 'sentiment': {
      const parts = [
        state.limit_up_count == null ? null : `涨停 ${state.limit_up_count}`,
        state.broken_rate == null ? null : `炸板 ${num(state.broken_rate, 0)}%`,
        state.max_height == null ? null : `最高 ${state.max_height} 板`,
      ].filter(Boolean);
      return parts.length ? parts.join(' · ') : '—';
    }
    default:
      return state.premium == null ? '中证1000 对 沪深300' : `昨涨停溢价 ${pct(state.premium, 1)}`;
  }
}

/** One dimension as a spectrum: every possible state is visible, the current one lit. */
function Meter({ dimension, active }: { dimension: Dimension; active: string }) {
  const states = Object.entries(REGIME_STATES[dimension]);
  return (
    <span className="regime-meter" aria-hidden="true">
      {states.map(([key, meta]) => (
        <i key={key} className={key === active ? 'on' : ''} style={{ '--c': `var(${meta.colour})` } as React.CSSProperties}>{meta.short}</i>
      ))}
    </span>
  );
}

interface RegimeBannerProps {
  state: RegimeState;
  eyebrow?: string;
  actions?: ReactNode;
  compact?: boolean;
}

/** The market's current position on all four regime dimensions, with the numbers behind it. */
export function RegimeBanner({ state, eyebrow = 'MARKET REGIME', actions, compact = false }: RegimeBannerProps) {
  const trend = stateMeta('trend', state.trend);
  return (
    <section className={`regime-banner ${compact ? 'is-compact' : ''}`} style={{ '--trend': `var(${trend.colour})` } as React.CSSProperties} aria-label={`市场周期：${state.label}`}>
      <div className="regime-main">
        <span className="eyebrow">{eyebrow} · <span className="num">{state.date}</span></span>
        <h2 className="display regime-label">
          {state.label.split(' · ').map((part, index) => (
            <span key={part}>{index > 0 && <i aria-hidden="true"> · </i>}<span className="regime-part">{part}</span></span>
          ))}
        </h2>
        {!compact && state.explanation.length > 0 && (
          <ul className="regime-why">
            {state.explanation.map(line => <li key={line}>{line}</li>)}
          </ul>
        )}
        {actions && <div className="regime-actions">{actions}</div>}
      </div>
      <dl className="regime-dims">
        {DIMENSIONS.map(({ key, label }) => {
          const meta = stateMeta(key, state[key]);
          return (
            <div key={key} className="regime-dim" style={{ '--c': `var(${meta.colour})` } as React.CSSProperties}>
              <dt>{label}</dt>
              <dd>
                <b>{meta.label}</b>
                <Meter dimension={key} active={state[key]} />
                <small className="num">{evidence(state, key)}</small>
              </dd>
            </div>
          );
        })}
      </dl>
    </section>
  );
}

/** Which history a research answer was computed on: real synced bars or the synthetic set. */
export function HistoryChip({ provenance }: { provenance: DataProvenance }) {
  const sim = provenance.mode === 'SIM';
  return (
    <span className={`history-chip ${sim ? 'sim' : 'live'}`} title={provenance.detail}>
      <Database size={13} />
      {sim ? '合成历史' : '真实历史'} · <b className="num">{provenance.coverage}</b> 只 · 截至 <span className="num">{provenance.trade_date}</span>
    </span>
  );
}
