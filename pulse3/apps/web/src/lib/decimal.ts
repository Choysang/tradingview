/**
 * Decimal-string helpers (contract Decimal = plain string, up to 12 fractional digits).
 * Why: money/price/ratio cross the API as strings; the browser must compare and
 * format them without binary floats. Invariants: null/invalid stays null (UNKNOWN),
 * never 0. Not covered: arithmetic beyond compare/format (the browser never
 * computes scores, returns or indicators).
 */
const SCALE = 12;
const RE = /^(-?)(0|[1-9][0-9]*)(?:\.([0-9]{1,12}))?$/;

/** Parse to a bigint scaled by 1e12, or null when the string is not a contract decimal. */
export function parseDecimal(value: string | null | undefined): bigint | null {
  if (value == null) return null;
  const m = RE.exec(value);
  if (!m) return null;
  const sign = m[1];
  const int = m[2] ?? '0';
  const frac = m[3] ?? '';
  const scaled = BigInt(int + frac.padEnd(SCALE, '0'));
  return sign === '-' ? -scaled : scaled;
}

/** -1 | 0 | 1, or null when either side is missing/invalid. */
export function compareDecimal(a: string | null | undefined, b: string | null | undefined): -1 | 0 | 1 | null {
  const x = parseDecimal(a);
  const y = parseDecimal(b);
  if (x === null || y === null) return null;
  return x < y ? -1 : x > y ? 1 : 0;
}

/** Fixed digits, round half to even. Returns null for missing/invalid input. */
export function toFixed(value: string | null | undefined, digits: number): string | null {
  const scaled = parseDecimal(value);
  if (scaled === null) return null;
  const unit = 10n ** BigInt(SCALE - digits);
  const neg = scaled < 0n;
  const abs = neg ? -scaled : scaled;
  let q = abs / unit;
  const r = abs % unit;
  const half = unit / 2n;
  if (r > half || (r === half && q % 2n === 1n)) q += 1n;
  const s = q.toString().padStart(digits + 1, '0');
  const body = digits > 0 ? `${s.slice(0, -digits)}.${s.slice(-digits)}` : s;
  return neg && q !== 0n ? `-${body}` : body;
}

/** Ratio/probability string (0..1) shown as percent. */
export function toPercent(value: string | null | undefined, digits = 1): string | null {
  const scaled = parseDecimal(value);
  if (scaled === null) return null;
  const neg = scaled < 0n;
  const abs = (neg ? -scaled : scaled) * 100n;
  const padded = abs.toString().padStart(SCALE + 1, '0');
  const dec = `${padded.slice(0, -SCALE)}.${padded.slice(-SCALE)}`;
  const out = toFixed(neg ? `-${dec}` : dec, digits);
  return out === null ? null : `${out}%`;
}

/** Display number for chart libraries only (never fed back into any decision). */
export function toChartNumber(value: string | null | undefined): number | null {
  if (value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
