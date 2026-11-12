/** Extension pages owned by work group A (PEX cards). Each entry adds one route and one nav item. */
import type { ReactElement } from 'react';
import { MarketPage } from '../pages/ext/a/MarketPage';
import { RegimePage } from '../pages/ext/a/RegimePage';
import { SectorDetailPage } from '../pages/ext/a/SectorDetailPage';
import { TrackRecordPage } from '../pages/ext/a/TrackRecordPage';

export interface ExtRoute {
  path: string;
  label: string;
  element: ReactElement;
}

export const routesA: ExtRoute[] = [
  { path: 'market', label: '市场总览', element: <MarketPage /> },
  { path: 'regime', label: '市场周期', element: <RegimePage /> },
  { path: 'track-record', label: '榜单战绩', element: <TrackRecordPage /> },
  { path: 'sector-detail', label: '板块详情', element: <SectorDetailPage /> },
];
