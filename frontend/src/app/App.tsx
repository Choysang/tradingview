import { lazy, Suspense, useEffect } from 'react';
import { useMarketStream } from '../core/live';
import { useWorkspace } from '../core/store';
import { Skeleton } from '../kit/primitives';
import { Aurora, Rail, Ribbon, Toaster } from '../features/shell/Shell';
import CommandPalette from '../features/command/CommandPalette';
import { useHashSync, useShortcuts } from './router';
import { viewById } from './views';

const StockSheet = lazy(() => import('../features/stock/StockSheet'));
const SourcesDialog = lazy(() => import('../features/settings/SourcesDialog'));

function PageFallback() {
  return (
    <div className="page-inner" aria-busy="true">
      <Skeleton height={44} width={280} />
      <Skeleton height={320} radius={14} />
    </div>
  );
}

export default function App() {
  useMarketStream();
  useHashSync();
  useShortcuts();
  const view = useWorkspace(state => state.view);
  const density = useWorkspace(state => state.density);
  const stock = useWorkspace(state => state.stock);
  const sourcesOpen = useWorkspace(state => state.sourcesOpen);
  const Current = viewById(view).component;

  // Hash navigation, back/forward and the palette all land here: start each view at its top.
  useEffect(() => { document.getElementById('main')?.scrollTo({ top: 0 }); }, [view]);

  return (
    <div className="app" data-density={density}>
      <a className="skip-link" href="#main" onClick={event => { event.preventDefault(); document.getElementById('main')?.focus(); }}>跳到主内容</a>
      <Aurora />
      <Rail />
      <div className="stage">
        <Ribbon />
        <main className="page" id="main" tabIndex={-1}>
          <Suspense fallback={<PageFallback />}>
            <Current />
          </Suspense>
        </main>
      </div>
      <Suspense>{stock && <StockSheet code={stock} />}</Suspense>
      <Suspense>{sourcesOpen && <SourcesDialog />}</Suspense>
      <CommandPalette />
      <Toaster />
    </div>
  );
}
