/** Pages of the legacy-feature backfill (docs/LEGACY_TRIAGE.md). Each entry adds one route and one nav item. */
import type { ReactElement } from 'react';
import { ThemeMapPage } from '../pages/bf/board/ThemeMapPage';
import { Batch6Page } from '../pages/bf/batch6/Batch6Page';
import { FusionPage } from '../pages/bf/fusion/FusionPage';
import { ForwardLogPage } from '../pages/bf/fwdlog/ForwardLogPage';
import { BatchComparePage } from '../pages/bf/lab/BatchComparePage';
import { LedgerPage } from '../pages/bf/lab/LedgerPage';

export interface ExtRoute {
  path: string;
  label: string;
  element: ReactElement;
}

export const routesBf: ExtRoute[] = [
  { path: 'themes', label: '题材图谱', element: <ThemeMapPage /> },
  { path: 'lab/ledger', label: '历史实验台账', element: <LedgerPage /> },
  { path: 'lab/batch', label: '批量对比', element: <BatchComparePage /> },
  { path: 'lab/fwdlog', label: '前向实验日志', element: <ForwardLogPage /> },
  { path: 'lab/fusion', label: '融合主书', element: <FusionPage /> },
  { path: 'lab/batch6', label: '短线研究（第六批）', element: <Batch6Page /> },
];
