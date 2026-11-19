/**
 * Bear-mode banner (legacy row #13): shown on the market page when the regime timeline's trend label is `bear`.
 * Rules: never shown on a label that rests on `insufficient_history` (the default label there is not a measurement) or when the
 * regime is unavailable. It is a state label, not a trading rule: ledger E016 (the old "bear mode" that went to cash below the
 * 60-day line) ended `mixed` (it dodged the decline and the rebound alike), and the tooltip says so.
 */
import { Badge } from '../../../components/Badges';
import type { Regime } from '../../ext/a/types';
import './fillm.css';

export const BEAR_TOOLTIP = 'E016（指数60日线闸门，即旧站熊市模式）的结论是 mixed：躲过下跌也躲过反弹，单边熊有效，震荡与反弹无效。'
  + '这里的“熊市”只是市场周期的状态标签，不是交易规则，也不建议据此空仓。';

export interface BearBannerModel { date: string; through: string | null }

/** The banner model, or null when it must not show. */
export function bearBannerOf(r: Regime | null | undefined): BearBannerModel | null {
  if (!r || !r.available || !r.current) return null;
  if (r.current.trend !== 'bear') return null;
  if (r.current.basis.trend !== 'ok') return null;       // insufficient_history / missing_index: the label is a default, not a measurement
  return { date: r.current.date, through: r.index?.through ?? null };
}

export function BearBanner({ regime }: { regime: Regime | null | undefined }) {
  const m = bearBannerOf(regime);
  if (!m) return null;
  return (
    <section className="card fm-bear" role="status" aria-label="熊市状态提示">
      <div className="ea-head">
        <h3>市场处于熊市状态</h3>
        <Badge tone="warn" title={BEAR_TOOLTIP}>状态标签 · 非交易规则</Badge>
        <span className="muted small">{m.date}{m.through ? `（指数数据截至 ${m.through}）` : ''}</span>
      </div>
      <p className="small" title={BEAR_TOOLTIP}>
        指数收盘低于 60 日线、60 日线 20 日斜率向下且站上 20 日线的个股占比偏低。这是描述当天市场结构的标签；旧站的“熊市模式空仓”在研究账本 E016 中没有被证明有效（mixed），详见悬停说明。
      </p>
    </section>
  );
}
