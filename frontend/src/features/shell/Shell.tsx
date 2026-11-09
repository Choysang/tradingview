import { useEffect, useState } from 'react';
import { Command, Database, Search, X } from 'lucide-react';
import { navigate } from '../../app/router';
import { VIEWS, viewById } from '../../app/views';
import { clock, num, pct } from '../../core/format';
import { useMarket, useWorkspace } from '../../core/store';
import { moodBand, moodHue } from './mood';
import './shell.css';

/** The ambient field. Its hue follows market sentiment: the room warms as the tape heats up. */
export function Aurora() {
  const score = useMarket(state => state.overview?.sentiment_score);
  useEffect(() => {
    document.documentElement.style.setProperty('--mood-h', moodHue(score).toFixed(1));
  }, [score]);
  return (
    <div className="aurora" aria-hidden="true">
      <i />
      <i />
      <i />
      <span className="grain" />
    </div>
  );
}

function Mark() {
  return (
    <svg className="mark" viewBox="0 0 32 32" aria-hidden="true">
      <rect x="1" y="1" width="30" height="30" rx="9" />
      <path className="mark-base" d="M6 18h5l2.5-7 4 12 3-8 1.5 3H26" />
      <path className="mark-pulse" d="M6 18h5l2.5-7 4 12 3-8 1.5 3H26" pathLength={100} />
    </svg>
  );
}

export function Rail() {
  const view = useWorkspace(state => state.view);
  const watchCount = useMarket(state => state.watchlist.length);
  return (
    <nav className="rail" aria-label="主导航">
        <button className="rail-brand" onClick={() => navigate('overview')} aria-label="Pulse 市场总览">
          <Mark />
        </button>
        <ul className="rail-list">
          {VIEWS.map((entry, index) => (
            <li key={entry.id}>
              <button
                className="rail-item"
                aria-current={view === entry.id ? 'page' : undefined}
                onClick={() => navigate(entry.id)}
                data-tip={`${entry.label} · ${index + 1}`}
                data-tip-side="right"
              >
                <entry.icon size={19} strokeWidth={1.6} />
                <span>{entry.short}</span>
                {entry.id === 'watchlist' && watchCount > 0 && <em className="num">{watchCount}</em>}
              </button>
            </li>
          ))}
        </ul>
        <div className="rail-foot">
          <button className="rail-item" onClick={() => useWorkspace.setState({ sourcesOpen: true })} data-tip="数据来源与质量" data-tip-side="right">
            <Database size={18} strokeWidth={1.6} />
            <span>数据</span>
          </button>
        </div>
    </nav>
  );
}

function StreamStatus() {
  const connection = useMarket(state => state.connection);
  const asOf = useMarket(state => state.asOf);
  const label = { connecting: '连接中', live: '实时', reconnecting: '重连中', stale: '数据静止' }[connection];
  return (
    <span className={`stream ${connection}`} role="status" aria-label={`数据流${label}`}>
      <i />
      <span className="num">{connection === 'live' ? clock(asOf) : label}</span>
    </span>
  );
}

function ModeBadge() {
  const overview = useMarket(state => state.overview);
  const provenance = overview?.provenance;
  const mode = provenance?.mode ?? '—';
  const fallback = provenance?.status === 'fallback';
  const stale = provenance?.status === 'stale';
  return (
    <button
      className={`mode ${mode === 'LIVE' ? 'live' : 'sim'} ${fallback || stale ? 'fallback' : ''}`}
      onClick={() => useWorkspace.setState({ sourcesOpen: true })}
      data-tip={stale ? provenance?.detail : provenance?.source ?? '数据来源'}
    >
      <span className="num">{mode}</span>
      <span className="mode-label">{mode === 'LIVE' ? '实盘' : '模拟'}{fallback ? ' · 回退' : stale ? ' · 过期' : ''}</span>
    </button>
  );
}

function Vitals() {
  const overview = useMarket(state => state.overview);
  if (!overview) return <div className="vitals" aria-hidden="true" />;
  const band = moodBand(overview.sentiment_score);
  const items = [
    { label: '涨停', value: String(overview.total_limit_up), tone: 'up' },
    { label: '炸板率', value: pct(overview.broken_rate, 1, false), tone: '' },
    { label: '高度', value: `${overview.max_limit_height}板`, tone: 'gold' },
    { label: '涨/跌', value: `${overview.breadth.up}/${overview.breadth.down}`, tone: '' },
  ];
  return (
    <dl className="vitals">
      <div className="vital mood-vital">
        <dt>情绪</dt>
        <dd><i className="mood-dot" /><span className="num">{num(overview.sentiment_score, 1)}</span><span className="mood-word">{band.word}</span></dd>
      </div>
      {items.map(item => (
        <div className="vital" key={item.label}>
          <dt>{item.label}</dt>
          <dd className={`num ${item.tone}`}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Ribbon() {
  const view = useWorkspace(state => state.view);
  const entry = viewById(view);
  return (
    <header className="ribbon">
      <span className="eyebrow ribbon-crumb">PULSE<b>/</b>{entry.code}</span>
      <Vitals />
      <div className="ribbon-actions">
        <button className="search-pill" onClick={() => useWorkspace.setState({ commandOpen: true })} aria-label="搜索股票与指令">
          <Search size={15} />
          <span>搜索股票、问句或指令</span>
          <kbd><Command size={10} />K</kbd>
        </button>
        <StreamStatus />
        <ModeBadge />
      </div>
    </header>
  );
}

export function Toaster() {
  const toasts = useWorkspace(state => state.toasts);
  return (
    <div className="toaster" aria-live="polite">
      {toasts.map(toast => (
        <div key={toast.id} className={`toast ${toast.tone ?? ''}`} role={toast.tone === 'error' ? 'alert' : 'status'}>
          <span>{toast.text}</span>
          <button aria-label="关闭通知" onClick={() => useWorkspace.setState(state => ({ toasts: state.toasts.filter(item => item.id !== toast.id) }))}>
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}

/** Page header used by every view: display title, one-line purpose, actions. */
export function PageHead({ title, lead, children }: { title: string; lead: string; children?: React.ReactNode }) {
  const [shown, setShown] = useState(false);
  useEffect(() => setShown(true), []);
  return (
    <div className={`page-head ${shown ? 'is-shown' : ''}`}>
      <div>
        <h1 className="display">{title}</h1>
        <p>{lead}</p>
      </div>
      {children && <div className="page-head-actions">{children}</div>}
    </div>
  );
}
