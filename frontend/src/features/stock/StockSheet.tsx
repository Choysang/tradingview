import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Copy, FlaskConical, RefreshCw, Star, X } from 'lucide-react';
import { navigate } from '../../app/router';
import { api } from '../../core/api';
import type { StockDetailResponse, StockTickRecord } from '../../core/contract';
import { hm, money, num, pct } from '../../core/format';
import { useLive } from '../../core/live';
import { useMarket, useWorkspace } from '../../core/store';
import { Num, Seg, Skeleton } from '../../kit/primitives';
import { Identity } from '../screener/ResultTable';
import type { Oscillator, Overlay } from './KChart';
import './stock.css';

const KChart = lazy(() => import('./KChart'));
const Analogs = lazy(() => import('./Analogs'));

/** Escape closes the sheet unless a layer above it (the palette) owns the key. */
function useEscape(onEscape: () => void) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const { commandOpen, sourcesOpen } = useWorkspace.getState();
      if (event.key === 'Escape' && !event.defaultPrevented && !commandOpen && !sourcesOpen) onEscape();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onEscape]);
}

/** Keep Tab focus inside the modal sheet, as aria-modal promises. */
function trapFocus(event: React.KeyboardEvent<HTMLElement>) {
  if (event.key !== 'Tab') return;
  const focusable = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input, select, [tabindex]:not([tabindex="-1"])')]
    .filter(element => element.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

function Stats({ record }: { record: StockTickRecord }) {
  // Real daily bars back-fill the yearly limit-up count; the snapshot value is the fallback.
  const limitUps = record.zt_count_1y_bar ?? record.zt_count_1y;
  const items: [string, string][] = [
    ['成交额', money(record.amount_today)],
    ['今日换手', pct(record.turnover_today, 2, false)],
    ['昨换比', num(record.turnover_ratio_yd, 2)],
    ['量比', num(record.volume_ratio, 2)],
    ['自由流通', money(record.free_float_mkt_cap)],
    ['首封 / 末封', `${hm(record.first_seal_time)} / ${hm(record.last_seal_time)}`],
    ['封单额', money(record.seal_order_amount)],
    ['封成比', pct(record.seal_to_amount_ratio, 1, false)],
    ['炸板次数', record.open_board_count == null ? '—' : `${record.open_board_count} 次`],
    ['一年涨停', limitUps == null ? '—' : `${limitUps} 次`],
    ['封板率', pct(record.seal_success_rate_1y, 1, false)],
    ['距 60 日高', pct(record.dist_to_60d_high, 1)],
  ];
  return (
    <dl className="sheet-stats">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd className="num">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Factors({ record }: { record: StockTickRecord }) {
  return (
    <div className="factor-bars">
      {record.factors.map(factor => (
        <div className="factor" key={factor.key}>
          <div className="factor-head">
            <span>{factor.label}<small className="num"> ×{(factor.weight * 100).toFixed(0)}%</small></span>
            <b className="num">{factor.score == null ? '—' : factor.score.toFixed(0)}</b>
          </div>
          <span className="factor-track"><i style={{ width: `${factor.score == null ? 0 : Math.max(2, Math.min(100, factor.score))}%` }} /></span>
          <p>{factor.explanation}</p>
        </div>
      ))}
    </div>
  );
}

function Chart({ detail }: { detail: StockDetailResponse }) {
  const [overlay, setOverlay] = useState<Overlay>('ma');
  const [oscillator, setOscillator] = useState<Oscillator>('macd');
  return (
    <section className="sheet-block">
      <div className="sheet-block-head">
        <h3>日 K <small className="faint num">{detail.bars.length} 根</small></h3>
        <div className="sheet-controls">
          <Seg<Overlay> label="主图叠加" value={overlay} onChange={setOverlay} options={[{ value: 'ma', label: 'MA' }, { value: 'boll', label: 'BOLL' }, { value: 'none', label: '无' }]} />
          <Seg<Oscillator> label="副图指标" value={oscillator} onChange={setOscillator} options={[{ value: 'macd', label: 'MACD' }, { value: 'kdj', label: 'KDJ' }, { value: 'rsi', label: 'RSI' }]} />
        </div>
      </div>
      {overlay === 'ma' && (
        <div className="legend ma-legend">
          <span><i style={{ background: 'var(--text-0)' }} />MA5</span>
          <span><i style={{ background: 'var(--gold)' }} />MA10</span>
          <span><i style={{ background: 'var(--accent)' }} />MA20</span>
          <span><i style={{ background: 'var(--cyan)' }} />MA60</span>
        </div>
      )}
      <Suspense fallback={<Skeleton height={400} radius={10} />}>
        <KChart bars={detail.bars} indicators={detail.indicators} overlay={overlay} oscillator={oscillator} />
      </Suspense>
      <p className={`provenance-note ${detail.history_provenance.mode === 'SIM' ? 'sim' : ''}`}>
        {detail.history_provenance.mode === 'SIM' ? '合成日线 · ' : ''}{detail.history_provenance.source} · {detail.history_provenance.detail}
      </p>
    </section>
  );
}

export default function StockSheet({ code }: { code: string }) {
  const close = useMemo(() => () => useWorkspace.getState().openStock(null), []);
  useEscape(close);
  // Refetch history when the data mode flips, so a LIVE quote never sits on SIM bars.
  const mode = useMarket(state => state.mode);
  const detail = useLive(signal => api.stock(code, signal), [code, mode], false);
  const quote = useLive(signal => api.query({ codes: [code], exclude_st: false, exclude_new: false, include_suspended: true, limit: 1 }, signal), [code]);
  const watched = useMarket(state => state.watchlist.includes(code));
  const closeButton = useRef<HTMLButtonElement>(null);
  const record = quote.data?.records[0] ?? detail.data?.stock;

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeButton.current?.focus();
    return () => previous?.focus?.();
  }, [code]);

  const toggleWatch = () => {
    if (!record) return;
    useMarket.getState().toggleWatch(code)
      .then(added => useWorkspace.getState().notify(added ? `已加入自选：${record.name}` : `已移出自选：${record.name}`, 'ok'))
      .catch(error => useWorkspace.getState().notify(`自选保存失败：${error.message}`, 'error'));
  };

  const copy = () => {
    navigator.clipboard?.writeText(code).then(() => useWorkspace.getState().notify(`已复制 ${code}`, 'ok'));
  };

  const backtest = () => {
    sessionStorage.setItem('pulse.backtest.code', code);
    useWorkspace.setState({ labTab: 'backtest', stock: null });
    navigate('lab');
  };

  return (
    <>
      <div className="scrim sheet-scrim" onClick={close} aria-hidden="true" />
      <aside className="sheet" role="dialog" aria-modal="true" aria-label={record ? `${record.name} 详情` : '个股详情'} onKeyDown={trapFocus}>
        <header className="sheet-head">
          {record ? (
            <div className="sheet-title">
              <div className="sheet-name">
                <h2>{record.name}</h2>
                <span className="num faint">{record.code}</span>
                {record.board !== '10cm' && <span className="badge cyan">{record.board}</span>}
                <Identity record={record} />
              </div>
              <div className="sheet-price">
                <Num value={record.price} format={value => num(value, 2)} toneOf={record.pct_chg} signed className="sheet-last" />
                <Num value={record.pct_chg} format={value => pct(value)} signed className="sheet-pct" />
                {record.composite_score != null && <span className="sheet-score">综合分 <b className="num">{record.composite_score.toFixed(0)}</b></span>}
              </div>
            </div>
          ) : <Skeleton height={56} width={260} />}
          <div className="sheet-actions">
            <button className={`btn icon ${watched ? 'is-watched' : ''}`} onClick={toggleWatch} aria-pressed={watched} data-tip={watched ? '移出自选' : '加入自选'}><Star size={15} fill={watched ? 'currentColor' : 'none'} /></button>
            <button className="btn icon" onClick={copy} data-tip="复制代码"><Copy size={15} /></button>
            <button className="btn icon" onClick={backtest} data-tip="用此股回测"><FlaskConical size={15} /></button>
            <button className="btn icon" onClick={detail.refresh} data-tip="刷新日线"><RefreshCw size={15} /></button>
            <button className="btn icon" onClick={close} ref={closeButton} aria-label="关闭详情（Esc）"><X size={16} /></button>
          </div>
        </header>
        <div className="sheet-body">
          {detail.error && !detail.data && <p className="error-note">详情加载失败：{detail.error}</p>}
          {detail.data ? (
            <>
              {detail.data.patterns.length > 0 && (
                <div className="sheet-patterns">
                  {detail.data.patterns.map(hit => (
                    <span key={hit.pattern_id} className="pattern-pill" title={hit.note}>
                      <i />{hit.note.length > 26 ? hit.note.slice(0, 26) + '…' : hit.note}
                    </span>
                  ))}
                </div>
              )}
              <Chart detail={detail.data} />
              <Suspense fallback={<Skeleton height={240} radius={10} />}>
                <Analogs code={code} />
              </Suspense>
              {record && (
                <section className="sheet-block">
                  <h3>盘面</h3>
                  <Stats record={record} />
                </section>
              )}
              {record && record.factors.length > 0 && (
                <section className="sheet-block">
                  <h3>因子拆解 <small className="faint">固定权重，缺数据的维度不计入</small></h3>
                  <Factors record={record} />
                </section>
              )}
              {detail.data.observations.length > 0 && (
                <section className="sheet-block">
                  <h3>事实观察</h3>
                  <ul className="observations">
                    {detail.data.observations.map(text => <li key={text}>{text}</li>)}
                  </ul>
                </section>
              )}
              {record && record.theme_tags.length > 0 && (
                <div className="sheet-themes">
                  {record.theme_tags.map(theme => (
                    <button key={theme} className="chip" onClick={() => { useWorkspace.getState().replaceFilters({ selected_themes: [theme] }, `#${theme}`); close(); navigate('screener'); }}>#{theme}</button>
                  ))}
                </div>
              )}
              <p className="provenance-note">快照：{detail.data.provenance.source} · {detail.data.provenance.mode}</p>
            </>
          ) : !detail.error && (
            <div className="sheet-loading">
              <Skeleton height={20} width={200} />
              <Skeleton height={400} radius={10} />
              <Skeleton height={120} radius={10} />
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
