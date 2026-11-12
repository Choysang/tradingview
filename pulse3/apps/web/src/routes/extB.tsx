/** Extension pages owned by work group B (PEX cards). Each entry adds one route and one nav item. */
import type { ReactElement } from 'react';
import { LabPage } from '../pages/ext/b/LabPage';
import { PlaybooksPage } from '../pages/ext/b/PlaybooksPage';
import { ScreenPage } from '../pages/ext/b/ScreenPage';

export interface ExtRoute {
  path: string;
  label: string;
  element: ReactElement;
}

export const routesB: ExtRoute[] = [
  { path: 'screen', label: '问句选股', element: <ScreenPage /> },
  { path: 'playbooks', label: '战法库', element: <PlaybooksPage /> },
  { path: 'lab', label: '因子与形态', element: <LabPage /> },
];
