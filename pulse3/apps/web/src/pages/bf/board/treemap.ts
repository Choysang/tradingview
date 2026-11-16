/**
 * Squarified treemap layout (Bruls, Huizing, van Wijk) in a 0..100 x 0..100 box, plus the change -> colour scale.
 * Why: the theme map draws sector amount as area; this only places rectangles for numbers the server already computed.
 * Invariants: items with a missing or non-positive value are not placed (the page lists them separately); the order of the
 * result is by value descending, ties by key, so a re-render never shuffles tiles.
 */
export interface TreeItem { key: string; value: number }
export interface TreeRect { key: string; x: number; y: number; w: number; h: number }

function worst(row: number[], side: number): number {
  const s = row.reduce((a, b) => a + b, 0);
  const max = Math.max(...row);
  const min = Math.min(...row);
  return Math.max((side * side * max) / (s * s), (s * s) / (side * side * min));
}

export function squarify(items: readonly TreeItem[], box = { x: 0, y: 0, w: 100, h: 100 }): TreeRect[] {
  const list = items.filter((i) => Number.isFinite(i.value) && i.value > 0).sort((a, b) => b.value - a.value || a.key.localeCompare(b.key));
  const total = list.reduce((a, i) => a + i.value, 0);
  if (list.length === 0 || total <= 0) return [];
  const scale = (box.w * box.h) / total;
  const areas = list.map((i) => i.value * scale);
  const out: TreeRect[] = [];
  let { x, y, w, h } = box;
  let i = 0;
  while (i < list.length) {
    const side = Math.min(w, h);
    const row: number[] = [areas[i] as number];
    let j = i + 1;
    while (j < list.length && worst([...row, areas[j] as number], side) <= worst(row, side)) { row.push(areas[j] as number); j += 1; }
    const sum = row.reduce((a, b) => a + b, 0);
    if (w >= h) {                       // lay the row as a column on the left
      const colW = sum / h;
      let yy = y;
      row.forEach((a, k) => { const hh = a / colW; out.push({ key: (list[i + k] as TreeItem).key, x, y: yy, w: colW, h: hh }); yy += hh; });
      x += colW; w -= colW;
    } else {                            // lay the row as a strip on top
      const rowH = sum / w;
      let xx = x;
      row.forEach((a, k) => { const ww = a / rowH; out.push({ key: (list[i + k] as TreeItem).key, x: xx, y, w: ww, h: rowH }); xx += ww; });
      y += rowH; h -= rowH;
    }
    i = j;
  }
  return out;
}

const UP = [214, 51, 58];
const DOWN = [23, 147, 90];
const NEUTRAL = [107, 114, 128];
export const COLOR_FULL_PCT = 5;

/** Red = up, green = down (the app convention); depth saturates at +-5%. A missing change is grey, never "flat". */
export function changeColor(pct: number | null): string {
  if (pct == null || Number.isNaN(pct)) return `rgb(${NEUTRAL.join(',')})`;
  const t = Math.min(Math.abs(pct) / COLOR_FULL_PCT, 1);
  const base = pct >= 0 ? UP : DOWN;
  const mix = (c: number, n: number): number => Math.round(n + (c - n) * (0.35 + 0.65 * t));
  return `rgb(${base.map((c, k) => mix(c, (NEUTRAL[k] as number) * 0.9)).join(',')})`;
}
