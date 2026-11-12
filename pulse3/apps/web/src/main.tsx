import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

/**
 * Real mode first asks the API for the values the contract does not enumerate (slot key, horizons, taxonomies,
 * quote policy). They are put on window BEFORE the app modules are evaluated (config.ts reads them at import).
 * A failed fetch is not fatal: the built-in defaults and VITE_* overrides apply and the release bar shows the
 * resulting API errors. Mock mode never calls it.
 */
async function loadRuntimeConfig(): Promise<void> {
  if (import.meta.env.VITE_USE_MOCK === '1') return;
  try {
    const res = await fetch('/app-config.json', { cache: 'no-store' });
    if (res.ok) window.__PULSE_CONFIG__ = await res.json();
  } catch {
    /* defaults apply */
  }
}

const root = document.getElementById('root');
if (!root) throw new Error('#root not found');
void loadRuntimeConfig()
  .then(() => import('./App'))
  .then(({ App }) => {
    createRoot(root).render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  });
