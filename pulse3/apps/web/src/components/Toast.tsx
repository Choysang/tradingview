import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

export interface ToastItem {
  id: number;
  tone: 'info' | 'good' | 'bad';
  text: string;
}

interface ToastApi {
  push: (tone: ToastItem['tone'], text: string) => void;
}

const Ctx = createContext<ToastApi>({ push: () => undefined });
export const useToast = (): ToastApi => useContext(Ctx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((tone: ToastItem['tone'], text: string) => {
    const id = Date.now() + Math.random();
    setItems((s) => [...s.slice(-3), { id, tone, text }]);
    setTimeout(() => setItems((s) => s.filter((t) => t.id !== id)), 5000);
  }, []);
  const api = useMemo(() => ({ push }), [push]);
  return (
    <Ctx.Provider value={api}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast toast-${t.tone}`}>{t.text}</div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
