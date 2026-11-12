/**
 * App shell: navigation, synthetic-sample banner, update-channel state, multi-dimension
 * data-health pills (never a single green light) and the theme switch.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { NavLink, Outlet, useSearchParams } from 'react-router-dom';
import { routesA } from '../routes/extA';
import { routesB } from '../routes/extB';
import { routesC } from '../routes/extC';
import { dataStatusQuery } from '../api/keys';
import { USE_MOCK } from '../config';
import { useSyntheticSeen } from '../hooks/useSynthetic';
import { useTheme } from '../hooks/useTheme';
import { useUpdatesStatus } from '../hooks/useUpdates';
import { fmtAge } from '../lib/format';
import { Badge } from './Badges';

const BASE_NAV: Array<[string, string]> = [
  ['/', '首页'],
  ['/strategies', '策略与联赛'],
  ['/papers', '研究论文'],
  ['/portfolio', '账户与可行动'],
  ['/data', '数据状态'],
];
const NAV: Array<[string, string]> = [...BASE_NAV, ...[...routesA, ...routesB, ...routesC].map((r): [string, string] => [`/${r.path}`, r.label])];

async function mockSwitch(name: string): Promise<void> {
  await fetch(`/__mock/${name}`, { method: 'POST' });
}

function MockControls() {
  const qc = useQueryClient();
  const upd = useUpdatesStatus();
  const act = (name: string) => async (): Promise<void> => { await mockSwitch(name); upd.pollNow(); void qc.invalidateQueries({ queryKey: ['dataStatus'] }); };
  return (
    <span className="mock-controls" aria-label="合成样本控制">
      <button className="btn btn-small" onClick={act('publish')} title="在模拟服务端发布新版本并推送 decision.published">模拟新发布</button>
      <button className="btn btn-small" onClick={act('glitch')} title="推送一条重复事件和一条旧版本事件，页面应忽略">重复/乱序事件</button>
      <button className="btn btn-small" onClick={act('expire')} title="使游标过期，页面应整体重取">游标过期</button>
      <button className="btn btn-small" onClick={act('stale')} title="切换模拟服务端的数据过期状态">切换过期</button>
    </span>
  );
}

function GlobalHealth() {
  const q = useQuery(dataStatusQuery());
  const items = q.data?.data.items ?? [];
  if (!q.data) return <span className="muted small">数据源状态读取中…</span>;
  const count = (f: (i: (typeof items)[number]) => boolean): number => items.filter(f).length;
  const stale = count((i) => i.freshness_state === 'stale');
  const down = count((i) => i.runtime_state === 'unavailable' || i.runtime_state === 'auth_required');
  const off = count((i) => !i.enabled || i.runtime_state === 'paused' || i.runtime_state === 'unconfigured');
  return (
    <NavLink to="/data" className="health-pills" title="分别显示：已启用、运行状态、新鲜度。点击查看每个来源">
      <Badge tone="neutral">数据源 {items.length}</Badge>
      <Badge tone={stale ? 'bad' : 'good'}>过期 {stale}</Badge>
      <Badge tone={down ? 'bad' : 'good'}>异常 {down}</Badge>
      <Badge tone={off ? 'warn' : 'neutral'}>未启用 {off}</Badge>
    </NavLink>
  );
}

export function Shell() {
  const synthetic = useSyntheticSeen();
  const theme = useTheme();
  const upd = useUpdatesStatus();
  const [sp] = useSearchParams();
  const carry = sp.get('release');
  const themeLabel = theme.choice === 'auto' ? '跟随系统' : theme.choice === 'light' ? '浅色' : '深色';
  return (
    <div className="app">
      {synthetic && (
        <div className="synthetic-banner" role="note">
          <b>合成样本</b>
          <span>当前页面的所有股票、价格、排名和计划都是虚构的演示数据，不是真实行情，也不构成任何投资建议。</span>
          {USE_MOCK && <MockControls />}
        </div>
      )}
      {!upd.connected && (
        <div className="notice notice-bad" role="alert">更新通道中断（{upd.lastError}）。页面内容可能已过期，恢复后会自动补取。<button className="btn btn-small" onClick={upd.pollNow}>立即重试</button></div>
      )}
      <header className="topbar">
        <NavLink to={carry ? `/?release=${carry}` : '/'} className="brand" aria-label="Pulse 首页">
          <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="var(--accent)" /><path d="M5 18h5l3-8 5 14 3-9h6" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
          Pulse
        </NavLink>
        <nav className="nav" aria-label="主导航">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={carry && to === '/' ? `${to}?release=${carry}` : to} end={to === '/'} className={({ isActive }) => (isActive ? 'active' : '')}>{label}</NavLink>
          ))}
        </nav>
        <div className="topbar-right">
          <GlobalHealth />
          <span className="muted small" title="最近一次更新轮询">{upd.lastOkAt ? `更新 ${fmtAge(new Date(upd.lastOkAt).toISOString())}` : '更新中'}</span>
          <button className="btn btn-small btn-ghost" onClick={theme.cycle} aria-label={`主题：${themeLabel}`}>{themeLabel}</button>
        </div>
      </header>
      <main className="main"><Outlet /></main>
      <footer className="footer">
        平台输出的是基于规则与统计的研究结果，不构成投资建议，也不代替你下单。排名是同一目标下的相对顺序，除非标明「校准概率」，否则不是上涨概率。
      </footer>
    </div>
  );
}
