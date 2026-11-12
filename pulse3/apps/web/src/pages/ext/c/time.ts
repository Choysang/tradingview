/** Time helpers: contract instants are UTC `Z` strings; user-facing dates are Asia/Shanghai days. */
const DAY = 86_400_000;
const dayFmt = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });

/** ISO instant without milliseconds, e.g. 2026-11-12T01:02:03Z. */
export const isoSeconds = (ms: number): string => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
export const windowLastDays = (days: number, nowMs: number): { start: string; end: string } => ({ start: isoSeconds(nowMs - days * DAY), end: isoSeconds(nowMs) });
export const windowNextDays = (days: number, nowMs: number): { start: string; end: string } => ({ start: isoSeconds(nowMs), end: isoSeconds(nowMs + days * DAY) });

/** 'YYYY-MM-DD' of an instant, Shanghai calendar. */
export const shanghaiDate = (ms: number): string => dayFmt.format(new Date(ms));

/** Start of a Shanghai calendar day as a UTC instant; null for a malformed date. */
export function shanghaiDayStart(date: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const t = Date.parse(`${date}T00:00:00+08:00`);
  return Number.isNaN(t) ? null : isoSeconds(t);
}

/** Most recent Mon-Fri (Shanghai) on or before `nowMs`; holidays are not detected (the page then simply shows no reports). */
export function lastWeekday(nowMs: number): string {
  let t = nowMs;
  for (let i = 0; i < 7; i += 1) {
    const d = shanghaiDate(t);
    const dow = new Date(`${d}T00:00:00Z`).getUTCDay();
    if (dow !== 0 && dow !== 6) return d;
    t -= DAY;
  }
  return shanghaiDate(nowMs);
}
