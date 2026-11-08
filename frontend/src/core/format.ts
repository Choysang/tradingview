/** Presentation only: units come from the contract (亿元, percentage points). */

const formatters = new Map<number, Intl.NumberFormat>();

function formatter(digits: number) {
  let cached = formatters.get(digits);
  if (!cached) {
    cached = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    formatters.set(digits, cached);
  }
  return cached;
}

const known = (value: number | null | undefined): value is number => value != null && Number.isFinite(value);

export const DASH = '—';

export function num(value: number | null | undefined, digits = 2) {
  return known(value) ? formatter(digits).format(value) : DASH;
}

export function pct(value: number | null | undefined, digits = 2, signed = true) {
  if (!known(value)) return DASH;
  const sign = signed && value > 0 ? '+' : '';
  return `${sign}${formatter(digits).format(value)}%`;
}

/** Amounts arrive in 亿元; small ones read better in 万. */
export function money(value: number | null | undefined) {
  if (!known(value)) return DASH;
  if (Math.abs(value) >= 1) return `${formatter(value >= 100 ? 0 : 2).format(value)} 亿`;
  return `${formatter(0).format(value * 10000)} 万`;
}

export function tone(value: number | null | undefined): 'up' | 'down' | 'flat' {
  if (!known(value) || value === 0) return 'flat';
  return value > 0 ? 'up' : 'down';
}

export function clock(iso: string | null | undefined) {
  if (!iso) return DASH;
  const match = /T(\d{2}:\d{2}:\d{2})/.exec(iso);
  return match ? match[1] : iso.slice(0, 8);
}

export function hm(time: string | null | undefined) {
  return time ? time.slice(0, 5) : DASH;
}

export function signed(value: number | null | undefined, digits = 2) {
  if (!known(value)) return DASH;
  return `${value > 0 ? '+' : ''}${formatter(digits).format(value)}`;
}

/** Map a session clock HH:MM:SS onto trading minutes, collapsing the lunch break. */
export function sessionMinute(time: string) {
  const [h, m, s] = time.split(':').map(Number);
  const minutes = h * 60 + m + (s || 0) / 60;
  const open = 9 * 60 + 30;
  if (minutes <= 11 * 60 + 30) return Math.max(-15, minutes - open);
  if (minutes < 13 * 60) return 120;
  return 120 + Math.min(120, minutes - 13 * 60);
}
