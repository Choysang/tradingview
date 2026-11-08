import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { AlertTriangle, Inbox } from 'lucide-react';
import { tone } from '../core/format';

interface PanelProps {
  eyebrow?: string;
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  className?: string;
  bodyClass?: string;
  children: ReactNode;
  id?: string;
}

export function Panel({ eyebrow, title, subtitle, actions, className = '', bodyClass = '', children, id }: PanelProps) {
  const headingId = useId();
  return (
    <section className={`panel ${className}`} aria-labelledby={title ? headingId : undefined} id={id}>
      {(title || actions) && (
        <header className="panel-head">
          <div className="panel-title">
            {eyebrow && <span className="eyebrow">{eyebrow}</span>}
            {title && <h2 id={headingId}>{title}</h2>}
            {subtitle && <p>{subtitle}</p>}
          </div>
          {actions && <div className="panel-actions">{actions}</div>}
        </header>
      )}
      <div className={`panel-body ${bodyClass}`}>{children}</div>
    </section>
  );
}

interface NumProps {
  value: number | null | undefined;
  format: (value: number | null | undefined) => string;
  /** colour by sign of the value */
  signed?: boolean;
  /** colour by the sign of this value instead */
  toneOf?: number | null;
  className?: string;
}

/** A number that briefly glows red or green when it moves — the market's heartbeat at cell level. */
export function Num({ value, format, signed = false, toneOf, className = '' }: NumProps) {
  const previous = useRef(value);
  const [flash, setFlash] = useState<'' | 'flash-up' | 'flash-down'>('');
  useEffect(() => {
    const before = previous.current;
    previous.current = value;
    if (before == null || value == null || before === value) return;
    setFlash(value > before ? 'flash-up' : 'flash-down');
    const timer = window.setTimeout(() => setFlash(''), 700);
    return () => window.clearTimeout(timer);
  }, [value]);
  const colour = signed ? tone(toneOf ?? value) : '';
  return <span className={`num ${colour} ${flash} ${className}`}>{format(value)}</span>;
}

interface SegProps<T extends string> {
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (value: T) => void;
  label: string;
  full?: boolean;
}

export function Seg<T extends string>({ value, options, onChange, label, full }: SegProps<T>) {
  return (
    <div className={`seg ${full ? 'full' : ''}`} role="radiogroup" aria-label={label}>
      {options.map(option => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Toggle({ checked, onChange, children }: { checked: boolean; onChange: (next: boolean) => void; children: ReactNode }) {
  return (
    <button type="button" role="switch" aria-checked={checked} className="toggle" onClick={() => onChange(!checked)}>
      <span>{children}</span>
      <span className="track" aria-hidden="true" />
    </button>
  );
}

interface RangeProps {
  label: string;
  min: number;
  max: number;
  step?: number;
  low: number;
  high: number;
  format: (low: number, high: number) => string;
  onChange: (low: number, high: number) => void;
}

/** Two native range inputs stacked: keyboard and screen-reader support for free. */
export function Range({ label, min, max, step = 1, low, high, format, onChange }: RangeProps) {
  const span = max - min || 1;
  const left = ((Math.max(min, low) - min) / span) * 100;
  const right = ((Math.min(max, high) - min) / span) * 100;
  return (
    <div className="range">
      <div className="range-head">
        <span>{label}</span>
        <output>{format(low, high)}</output>
      </div>
      <div className="range-track">
        <span className="range-fill" style={{ left: `${left}%`, width: `${Math.max(0, right - left)}%` }} />
        <input type="range" aria-label={`${label}下限`} min={min} max={max} step={step} value={Math.min(low, max)} onChange={event => onChange(Math.min(Number(event.target.value), high), high)} />
        <input type="range" aria-label={`${label}上限`} min={min} max={max} step={step} value={Math.min(high, max)} onChange={event => onChange(low, Math.max(Number(event.target.value), low))} />
      </div>
    </div>
  );
}

export function Empty({ title, children, icon }: { title: string; children?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="empty">
      {icon ?? <Inbox size={22} strokeWidth={1.5} />}
      <strong>{title}</strong>
      {children && <span>{children}</span>}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div className="error-note" role="alert">
      <AlertTriangle size={16} />
      <span>{children}</span>
    </div>
  );
}

export function Skeleton({ height = 16, width = '100%', radius }: { height?: number; width?: number | string; radius?: number }) {
  return <span className="skeleton" style={{ display: 'block', height, width, borderRadius: radius }} aria-hidden="true" />;
}

/** Measure an element's content box; charts draw in real pixels instead of stretching a viewBox. */
export function useSize<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize(current => (current.width === Math.round(width) && current.height === Math.round(height) ? current : { width: Math.round(width), height: Math.round(height) }));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, size] as const;
}
