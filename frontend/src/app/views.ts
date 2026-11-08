/**
 * The view registry. Adding a screen is one entry here plus one folder under
 * `features/` — the rail, palette, shortcuts and router all read this list.
 */
import { lazy } from 'react';
import type { ComponentType, LazyExoticComponent } from 'react';
import { Bookmark, Compass, FlaskConical, LayoutGrid, Radar, Sparkles, Swords, Trophy, Waves } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { View } from '../core/store';
import OverviewView from '../features/overview/OverviewView';

export interface ViewEntry {
  id: View;
  label: string;
  short: string;
  code: string;
  description: string;
  icon: LucideIcon;
  component: ComponentType | LazyExoticComponent<ComponentType>;
}

export const VIEWS: ViewEntry[] = [
  { id: 'overview', label: '市场总览', short: '总览', code: 'OVERVIEW', description: '情绪、连板天梯、封板时钟与盘口异动', icon: LayoutGrid, component: OverviewView },
  { id: 'picks', label: '今日推荐', short: '推荐', code: 'PICKS', description: '按当前周期加权多套战法，给出短线、波段、长线与总榜', icon: Trophy, component: lazy(() => import('../features/picks/PicksView')) },
  { id: 'playbooks', label: '战法库', short: '战法', code: 'PLAYBOOKS', description: '主流战法的全历史回测、样本外检验与分周期表现', icon: Swords, component: lazy(() => import('../features/playbooks/PlaybooksView')) },
  { id: 'regime', label: '市场周期', short: '周期', code: 'REGIME', description: '牛熊震荡、增量存量、情绪周期与风格，以及各周期下有效的打法', icon: Compass, component: lazy(() => import('../features/regime/RegimeView')) },
  { id: 'screener', label: '问句选股', short: '选股', code: 'SCREENER', description: '用一句话或条件组合筛选全市场', icon: Sparkles, component: lazy(() => import('../features/screener/ScreenerView')) },
  { id: 'themes', label: '题材图谱', short: '题材', code: 'THEMES', description: '题材强度、成交与龙头', icon: Waves, component: lazy(() => import('../features/themes/ThemesView')) },
  { id: 'patterns', label: '形态雷达', short: '形态', code: 'PATTERNS', description: '经典 A 股选股形态的今日命中', icon: Radar, component: lazy(() => import('../features/patterns/PatternsView')) },
  { id: 'lab', label: '策略实验室', short: '实验', code: 'LAB', description: '确定性回测与因子 IC 检验', icon: FlaskConical, component: lazy(() => import('../features/lab/LabView')) },
  { id: 'watchlist', label: '自选观察', short: '自选', code: 'WATCHLIST', description: '关注标的的实时状态与异动', icon: Bookmark, component: lazy(() => import('../features/watchlist/WatchlistView')) },
];

export const viewById = (id: View) => VIEWS.find(entry => entry.id === id) ?? VIEWS[0];
