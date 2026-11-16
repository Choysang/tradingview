/** Display formatting of board lane figures (decimal strings from the server; missing stays missing, never 0). */
export const fmtYi = (v: string | null): string => {
  const n = v == null ? NaN : Number(v);
  if (Number.isNaN(n)) return '缺失';
  return n >= 1e8 ? `${(n / 1e8).toFixed(1)} 亿` : `${(n / 1e4).toFixed(0)} 万`;
};
export const fmtPct = (v: string | null): string => {
  const n = v == null ? NaN : Number(v);
  return Number.isNaN(n) ? '缺失' : `${n > 0 ? '+' : ''}${n.toFixed(2)}%`;
};
