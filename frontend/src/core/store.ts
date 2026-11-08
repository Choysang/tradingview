/**
 * Two stores with different lifetimes.
 *
 * `useMarket` mirrors the server: the latest tick, the overview and the
 * watchlist. It is never persisted — a reload must show the market as it is now.
 * `useWorkspace` is the researcher's desk: view, filters, presets, density.
 * It survives reloads so a half-built screen is never lost.
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { api } from './api';
import type { FilterInput } from './api';
import type { FactorLabRequest, MacroDashboardSummary } from './contract';

export type View = 'overview' | 'picks' | 'playbooks' | 'regime' | 'screener' | 'themes' | 'patterns' | 'lab' | 'watchlist';
export type PickHorizon = 'overall' | 'short' | 'swing' | 'long';
export type Connection = 'connecting' | 'live' | 'reconnecting' | 'stale';

interface MarketState {
  sequence: number;
  asOf: string | null;
  mode: 'LIVE' | 'SIM' | null;
  connection: Connection;
  lastTickAt: number;
  overview: MacroDashboardSummary | null;
  watchlist: string[];
  setWatchlist: (codes: string[]) => Promise<void>;
  toggleWatch: (code: string) => Promise<boolean>;
}

/** Stable empty list for selectors: a fresh `[]` per call makes React re-render forever. */
export const NONE: readonly never[] = Object.freeze([]);

export const useMarket = create<MarketState>((set, get) => ({
  sequence: 0,
  asOf: null,
  mode: null,
  connection: 'connecting',
  lastTickAt: 0,
  overview: null,
  watchlist: [],
  async setWatchlist(codes) {
    const previous = get().watchlist;
    set({ watchlist: codes });
    try {
      const saved = await api.saveWatchlist(codes);
      set({ watchlist: saved.codes });
    } catch (error) {
      set({ watchlist: previous });
      throw error;
    }
  },
  async toggleWatch(code) {
    const current = get().watchlist;
    const adding = !current.includes(code);
    await get().setWatchlist(adding ? [code, ...current] : current.filter(item => item !== code));
    return adding;
  },
}));

export interface Preset {
  id: string;
  name: string;
  filters: FilterInput;
  ask?: string;
}

export interface Toast {
  id: number;
  text: string;
  tone?: 'error' | 'ok';
}

interface WorkspaceState {
  view: View;
  stock: string | null;
  filters: FilterInput;
  ask: string;
  presets: Preset[];
  density: 'comfortable' | 'compact';
  commandOpen: boolean;
  sourcesOpen: boolean;
  labTab: 'backtest' | 'factor';
  factor: Partial<FactorLabRequest>;
  playbook: string | null;
  pickHorizon: PickHorizon;
  toasts: Toast[];
  setView: (view: View) => void;
  openStock: (code: string | null) => void;
  selectPlaybook: (id: string | null) => void;
  setFilters: (patch: FilterInput) => void;
  replaceFilters: (filters: FilterInput, ask?: string) => void;
  notify: (text: string, tone?: Toast['tone']) => void;
}

let toastId = 0;

export const useWorkspace = create<WorkspaceState>()(
  persist(
    set => ({
      view: 'overview',
      stock: null,
      filters: {},
      ask: '',
      presets: [],
      density: 'comfortable',
      commandOpen: false,
      sourcesOpen: false,
      labTab: 'backtest',
      factor: {},
      playbook: null,
      pickHorizon: 'overall',
      toasts: [],
      setView: view => set({ view }),
      openStock: stock => set({ stock }),
      selectPlaybook: playbook => set({ playbook }),
      setFilters: patch => set(state => ({ filters: { ...state.filters, ...patch } })),
      replaceFilters: (filters, ask = '') => set({ filters, ask }),
      notify: (text, tone) => {
        const id = ++toastId;
        set(state => ({ toasts: [...state.toasts.slice(-2), { id, text, tone }] }));
        window.setTimeout(() => set(state => ({ toasts: state.toasts.filter(item => item.id !== id) })), 4200);
      },
    }),
    {
      name: 'pulse.workspace.v3',
      partialize: state => ({
        view: state.view,
        filters: state.filters,
        ask: state.ask,
        presets: state.presets,
        density: state.density,
        labTab: state.labTab,
        factor: state.factor,
        playbook: state.playbook,
        pickHorizon: state.pickHorizon,
      }),
    },
  ),
);
