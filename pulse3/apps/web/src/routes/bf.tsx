/** Pages of the legacy-feature backfill (docs/LEGACY_TRIAGE.md). Each entry adds one route and one nav item. */
import type { ReactElement } from 'react';
import { ThemeMapPage } from '../pages/bf/board/ThemeMapPage';

export interface ExtRoute {
  path: string;
  label: string;
  element: ReactElement;
}

export const routesBf: ExtRoute[] = [
  { path: 'themes', label: '题材图谱', element: <ThemeMapPage /> },
];
