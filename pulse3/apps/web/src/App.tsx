import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { ApiError } from './api/client';
import { DiagnosticsProvider } from './components/Diagnostics';
import { Shell } from './components/Shell';
import { ToastProvider } from './components/Toast';
import { UpdatesProvider } from './hooks/useUpdates';
import { noteResponseMeta } from './hooks/useSynthetic';
import { HomePage } from './pages/HomePage';
import { SectorPage } from './pages/SectorPage';
import { StockPage } from './pages/StockPage';
import { StrategiesPage } from './pages/StrategiesPage';
import { DataStatusPage, PapersPage, PortfolioPage } from './pages/SmallPages';
import { routesA } from './routes/extA';
import { routesB } from './routes/extB';
import { routesC } from './routes/extC';

export const EXT_ROUTES = [...routesA, ...routesB, ...routesC];

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onSuccess: (data) => {
        const meta = (data as { meta?: { warnings?: Array<{ code: string }> } } | undefined)?.meta
          ?? (data as { pages?: Array<{ meta?: { warnings?: Array<{ code: string }> } }> } | undefined)?.pages?.[0]?.meta;
        noteResponseMeta(meta);
      },
    }),
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
        staleTime: 10_000,
      },
    },
  });
}

const client = makeQueryClient();

export function App() {
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <DiagnosticsProvider>
          <BrowserRouter>
            <UpdatesProvider>
              <Routes>
                <Route element={<Shell />}>
                  <Route index element={<HomePage />} />
                  <Route path="sector/:nodeId" element={<SectorPage />} />
                  <Route path="stock/:instrumentId" element={<StockPage />} />
                  <Route path="strategies" element={<StrategiesPage />} />
                  <Route path="papers" element={<PapersPage />} />
                  <Route path="portfolio" element={<PortfolioPage />} />
                  <Route path="data" element={<DataStatusPage />} />
                  {EXT_ROUTES.map((r) => <Route key={r.path} path={r.path} element={r.element} />)}
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Route>
              </Routes>
            </UpdatesProvider>
          </BrowserRouter>
        </DiagnosticsProvider>
      </ToastProvider>
    </QueryClientProvider>
  );
}
