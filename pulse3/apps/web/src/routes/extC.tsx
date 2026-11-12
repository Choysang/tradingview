/** Extension pages owned by work group C (PEX cards). Each entry adds one route and one nav item. */
import type { ReactElement } from 'react';
import { AlertsPage } from '../pages/ext/c/AlertsPage';
import { BooksPage } from '../pages/ext/c/BooksPage';
import { CalendarPage } from '../pages/ext/c/CalendarPage';
import { HealthPage } from '../pages/ext/c/HealthPage';
import { SeatsPage } from '../pages/ext/c/SeatsPage';

export interface ExtRoute {
  path: string;
  label: string;
  element: ReactElement;
}

export const routesC: ExtRoute[] = [
  { path: 'alerts', label: '告警', element: <AlertsPage /> },
  { path: 'calendar', label: '财经日历', element: <CalendarPage /> },
  { path: 'seats', label: '龙虎榜席位', element: <SeatsPage /> },
  { path: 'books', label: '前向主书', element: <BooksPage /> },
  { path: 'health', label: '数据健康', element: <HealthPage /> },
];
