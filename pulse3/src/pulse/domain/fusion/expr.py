"""Factor expression engine of the fusion lane (numpy, float32 grids of days x stocks).

Why: the round-5 fusion scorer (legacy triage batch 3, lane ``fusion``) evaluates about 430 factor expressions written in a
small Qlib-like language. The frozen expressions are data (``fusion/data``); this module is the only thing that gives them a
meaning, and the meaning is the one the old lab engine had: every operator below is specified by its docstring and by a
hand-computed case in ``tests/bf_fusion/test_expr_ops.py``.
Grammar: ``expr := cmp``; ``cmp := add (('>'|'<') add)?``; ``add := mul (('+'|'-') mul)*``; ``mul := un (('*'|'/') un)*``;
``un := '-' un | atom``; ``atom := number | $field | Func '(' args ')' | '(' expr ')'``. Nothing is ever ``eval``-ed.
Invariants: a grid row ``t`` only uses rows ``<= t`` (``Ref``/``Delta`` need ``d >= 1``); a missing bar is NaN; a rolling
window needs at least ``ceil(3/4 d)`` finite bars; every node result is float32 and a non-finite final value is NaN; rolling
sums accumulate in float64.
Not covered: loading data (``panel``), ranking or scoring (``scorer``), the level-safety check (``levelsafe``).
"""
from __future__ import annotations

import math
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping

import numpy as np

from .ranktie import rank_rows

F32 = np.float32
NAN32 = np.float32("nan")
MAX_WINDOW = 250
EPS = 1e-12
#: the fields an expression may name (``$name``); the panel provides every one of them
FIELDS = ("open", "high", "low", "close", "volume", "amount", "vwap", "turn", "ret", "mkt", "cap", "pct_chg",
          "is_limit_up", "is_limit_down", "touched_limit", "is_broken", "limit_streak", "prev_close", "cgo", "tk")


class ExprError(ValueError):
    """The expression text is not valid in the grammar (unknown field, bad window, trailing text)."""


# ---------------------------------------------------------------------------------------------------------------
# grid helpers
# ---------------------------------------------------------------------------------------------------------------
def _minp(d: int) -> int:
    """Finite bars a window of ``d`` rows needs: ``ceil(0.75 d)``, at least 1."""
    return max(1, math.ceil(0.75 * d))


def shift(x: np.ndarray, d: int) -> np.ndarray:
    """``d > 0``: the value ``d`` rows earlier; ``d < 0``: ``|d|`` rows later (labels only); ``d == 0``: a copy."""
    out = np.full(x.shape, np.nan, dtype=x.dtype)
    if d > 0:
        out[d:] = x[:-d]
    elif d < 0:
        out[:d] = x[-d:]
    else:
        out[:] = x
    return out


def nanmean_row(x: np.ndarray) -> np.ndarray:
    """Mean of the finite values of each row; a row without any gives 0 (never NaN), like the sum/count it is."""
    m = np.isfinite(x)
    return (np.where(m, x, 0).sum(axis=1, dtype=np.float64) / np.maximum(m.sum(axis=1), 1)).astype(F32)


def _wsum(x: np.ndarray, d: int) -> tuple[np.ndarray, np.ndarray]:
    """Rolling sum of the finite values and their count over the last ``d`` rows (float64). Rows before ``d-1`` are NaN/0."""
    m = np.isfinite(x)
    v = np.where(m, x, 0).astype(np.float64)
    z = np.zeros((1, x.shape[1]))
    cs = np.concatenate([z, np.cumsum(v, axis=0)])
    cm = np.concatenate([z, np.cumsum(m.astype(np.float64), axis=0)])
    s = np.full(x.shape, np.nan, dtype=np.float64)
    n = np.zeros(x.shape, dtype=np.float64)
    if d <= x.shape[0]:
        s[d - 1:] = cs[d:] - cs[:-d]
        n[d - 1:] = cm[d:] - cm[:-d]
    return s, n


def _valid_window(x: np.ndarray, d: int) -> np.ndarray:
    return _wsum(x, d)[1] >= _minp(d)


# ---------------------------------------------------------------------------------------------------------------
# time-series operators (all take a [T, N] float32 grid and a window ``d``)
# ---------------------------------------------------------------------------------------------------------------
def ts_sum(x, d):
    """Sum of the finite bars of the window; NaN unless ``ceil(3/4 d)`` bars are finite."""
    s, n = _wsum(x, d)
    return np.where(n >= _minp(d), s, np.nan).astype(F32)


def ts_mean(x, d):
    """Mean of the finite bars of the window (sum / finite count)."""
    s, n = _wsum(x, d)
    return np.where(n >= _minp(d), s / np.maximum(n, 1), np.nan).astype(F32)


def ts_var(x, d):
    """Sample variance (n - 1) of the finite bars; needs at least 2 and ``ceil(3/4 d)`` bars; never negative."""
    s, n = _wsum(x, d)
    s2, _ = _wsum(x.astype(np.float64) ** 2, d)
    var = (s2 - s * s / np.maximum(n, 1)) / np.maximum(n - 1, 1)
    return np.where(n >= max(2, _minp(d)), np.maximum(var, 0), np.nan).astype(F32)


def ts_std(x, d):
    """Square root of the float32 sample variance."""
    return np.sqrt(ts_var(x, d))


def ts_cov(x, y, d, corr=False):
    """Rolling covariance (n - 1) or correlation of two grids over the bars where both are finite (at least 3 bars).

    The correlation is NaN when the product of the variances is not clearly positive and is clipped to [-1, 1]."""
    m = np.isfinite(x) & np.isfinite(y)
    xx, yy = np.where(m, x, np.nan), np.where(m, y, np.nan)
    sx, n = _wsum(xx, d)
    sy, _ = _wsum(yy, d)
    sxy, _ = _wsum(xx.astype(np.float64) * yy.astype(np.float64), d)
    nn = np.maximum(n, 1)
    cov = (sxy - sx * sy / nn) / np.maximum(n - 1, 1)
    ok = n >= max(3, _minp(d))
    if not corr:
        return np.where(ok, cov, np.nan).astype(F32)
    sx2, _ = _wsum(xx.astype(np.float64) ** 2, d)
    sy2, _ = _wsum(yy.astype(np.float64) ** 2, d)
    vx = (sx2 - sx * sx / nn) / np.maximum(n - 1, 1)
    vy = (sy2 - sy * sy / nn) / np.maximum(n - 1, 1)
    den = np.sqrt(np.maximum(vx * vy, 0))
    good = ok & (den > 1e-12 * (1 + np.abs(vx) + np.abs(vy)))
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(good, cov / den, np.nan)
    return np.clip(r, -1, 1).astype(F32)


def _regress(x, d):
    """Slope, fitted value at the last bar and R^2 of the window regressed on the row index (full windows only; float64)."""
    T = x.shape[0]
    t = np.broadcast_to(np.arange(T, dtype=np.float64)[:, None], x.shape)
    m = np.isfinite(x)
    sx, n = _wsum(x, d)
    stx, _ = _wsum(np.where(m, t * x.astype(np.float64), np.nan), d)
    sx2, _ = _wsum(x.astype(np.float64) ** 2, d)
    start = t - (d - 1)
    sl_x = stx - start * sx
    sl, sl2 = d * (d - 1) / 2, (d - 1) * d * (2 * d - 1) / 6
    den = d * sl2 - sl * sl
    slope = (d * sl_x - sl * sx) / den
    mean_x, mean_l = sx / d, sl / d
    intercept = mean_x - slope * mean_l
    fit_last = intercept + slope * (d - 1)
    var_x = sx2 / d - mean_x ** 2
    var_l = sl2 / d - mean_l ** 2
    with np.errstate(invalid="ignore", divide="ignore"):
        r2 = np.where(var_x > 1e-18, np.clip(slope ** 2 * var_l / var_x, 0, 1), np.nan)
    full = n >= d
    return [np.where(full, v, np.nan) for v in (slope, fit_last, r2)]


def ts_slope(x, d):
    """Least-squares slope of the window against the row index; only a full window (every bar finite) counts."""
    return _regress(x, d)[0].astype(F32)


def ts_rsquare(x, d):
    """R^2 of that regression (0 for a flat window is NaN: a variance below 1e-18 gives no R^2)."""
    return _regress(x, d)[2].astype(F32)


def ts_resi(x, d):
    """Residual of the last bar: ``x - fitted value at the last bar``; full windows only."""
    return (x.astype(np.float64) - _regress(x, d)[1]).astype(F32)


def _extreme(x, d, largest: bool, want_pos: bool):
    """Rolling max/min and the 1-based position of it (oldest bar of the window = 1); a tie keeps the oldest bar."""
    fill = -np.inf if largest else np.inf
    xf = np.where(np.isfinite(x), x, fill).astype(F32)
    T = x.shape[0]
    best = np.full(xf.shape, fill, dtype=F32)
    pos = np.zeros(xf.shape, dtype=F32)
    for i in range(d - 1, -1, -1):  # lag i, oldest first, so a strict comparison keeps the oldest of equal values
        cand = np.full(xf.shape, fill, dtype=F32)
        if i < T:
            cand[i:] = xf[: T - i]
        better = cand > best if largest else cand < best
        best = np.where(better, cand, best)
        if want_pos:
            pos = np.where(better, F32(d - i), pos)
    ok = _valid_window(x, d)
    return np.where(ok, pos if want_pos else best, np.nan).astype(F32)


def ts_max(x, d):
    """Rolling maximum of the finite bars (NaN unless 3/4 of the window is finite)."""
    return _extreme(x, d, True, False)


def ts_min(x, d):
    """Rolling minimum of the finite bars."""
    return _extreme(x, d, False, False)


def ts_argmax(x, d):
    """1-based position of the window maximum, oldest bar = 1; the oldest wins a tie."""
    return _extreme(x, d, True, True)


def ts_argmin(x, d):
    """1-based position of the window minimum, oldest bar = 1; the oldest wins a tie."""
    return _extreme(x, d, False, True)


def _win_reduce(x, d, fn: Callable[[np.ndarray], np.ndarray], chunk: int = 600):
    out = np.full(x.shape, np.nan, dtype=F32)
    if d > x.shape[0]:
        return out
    for j in range(0, x.shape[1], chunk):
        w = np.lib.stride_tricks.sliding_window_view(x[:, j:j + chunk], d, axis=0)  # [T-d+1, n, d]
        ok = np.isfinite(w).sum(-1) >= _minp(d)
        out[d - 1:, j:j + chunk] = np.where(ok, fn(w), np.nan).astype(F32)
    return out


def _nanquantile(w: np.ndarray, q: float) -> np.ndarray:
    """Linear-interpolation quantile over the finite values of the last axis (all-NaN rows give NaN)."""
    s = np.sort(w, axis=-1)  # NaN sorts last
    k = np.isfinite(w).sum(-1)
    pos = np.maximum(k - 1, 0) * q
    lo = np.floor(pos).astype(np.int64)
    hi = np.minimum(lo + 1, np.maximum(k - 1, 0))
    frac = (pos - lo).astype(w.dtype)
    a = np.take_along_axis(s, lo[..., None], axis=-1)[..., 0]
    b = np.take_along_axis(s, hi[..., None], axis=-1)[..., 0]
    return np.where(k > 0, a + (b - a) * frac, np.nan)


def ts_quantile(x, d, q):
    """Rolling quantile ``q`` (linear interpolation between order statistics) of the finite bars."""
    return _win_reduce(x, d, lambda w: _nanquantile(w, q))


def ts_median(x, d):
    """Rolling median (quantile 0.5)."""
    return ts_quantile(x, d, 0.5)


def ts_mad(x, d):
    """Mean absolute deviation around the window mean (finite bars)."""
    def fn(w):
        with np.errstate(all="ignore"):
            mu = np.nanmean(w, axis=-1, keepdims=True)
            return np.nanmean(np.abs(w - mu), axis=-1)
    return _win_reduce(x, d, fn)


def ts_rank(x, d):
    """Percentile of the last value inside its window: (bars below + half of the bars equal, itself included) / finite bars."""
    T = x.shape[0]
    less = np.zeros(x.shape, dtype=F32)
    eq = np.zeros(x.shape, dtype=F32)
    cnt = np.zeros(x.shape, dtype=F32)
    for i in range(min(d, T)):
        past, cur = x[: T - i], x[i:]
        less[i:] += (past < cur).astype(F32)
        eq[i:] += (past == cur).astype(F32)
        cnt[i:] += np.isfinite(past).astype(F32)
    ok = _valid_window(x, d) & np.isfinite(x)
    return np.where(ok, (less + F32(0.5) * eq) / np.maximum(cnt, F32(1)), np.nan).astype(F32)


def ts_wma(x, d):
    """Linearly weighted mean over the finite bars; the newest bar weighs ``d``, the oldest 1."""
    T = x.shape[0]
    m = np.isfinite(x)
    xv = np.where(m, x, 0).astype(F32)
    out = np.zeros(x.shape, dtype=F32)
    wsum = np.zeros(x.shape, dtype=F32)
    for i in range(min(d, T)):
        w = F32(d - i)
        out[i:] += xv[: T - i] * w
        wsum[i:] += m[: T - i].astype(F32) * w
    return np.where(_valid_window(x, d), out / np.maximum(wsum, F32(EPS)), np.nan).astype(F32)


def ts_ema(x, d):
    """Exponential mean with ``alpha = 2/(d+1)``, adjust=False; a missing bar leaves the state unchanged."""
    a = F32(2 / (d + 1))
    out = np.full(x.shape, np.nan, dtype=F32)
    prev = np.full(x.shape[1], np.nan, dtype=F32)
    for t in range(x.shape[0]):
        cur = x[t]
        new = np.where(np.isfinite(prev), a * cur + (F32(1) - a) * prev, cur)
        prev = np.where(np.isfinite(cur), new, prev).astype(F32)
        out[t] = prev
    _, n = _wsum(x, d)
    return np.where(n >= _minp(d), out, np.nan).astype(F32)


def ts_delta(x, d):
    """``x - Ref(x, d)``."""
    return (x - shift(x, d)).astype(F32)


# ---------------------------------------------------------------------------------------------------------------
# cross-section operators (per row)
# ---------------------------------------------------------------------------------------------------------------
def cs_rank_stable(x):
    """Per-row ordinal rank of the finite values: ``(position + 0.5) / count``; equal values get consecutive ranks in column order
    (stable), non-finite stay NaN. Used where only pairs can tie (the IC of float16 ranks)."""
    m = np.isfinite(x)
    filled = np.where(m, x, np.inf)
    idx = np.argsort(filled, axis=1, kind="stable")
    r = np.empty(x.shape, dtype=F32)
    np.put_along_axis(r, idx, np.broadcast_to(np.arange(x.shape[1], dtype=F32), x.shape), axis=1)
    cnt = m.sum(axis=1, keepdims=True).astype(F32)
    return np.where(m, (r + F32(0.5)) / np.maximum(cnt, F32(1)), np.nan).astype(F32)


def cs_rank(x):
    """Per-row ordinal rank of the finite values like ``cs_rank_stable``, but a big tie group is ordered the way the old engine's
    unstable sort left it (``ranktie``): the frozen score depends on that order."""
    return rank_rows(x)


def cs_zscore(x):
    """Per-row (x - mean) / (sample std + 1e-9) over the finite values."""
    m = np.isfinite(x)
    cnt = m.sum(axis=1, keepdims=True)
    mu = np.where(m, x, 0).sum(axis=1, keepdims=True, dtype=np.float32) / np.maximum(cnt, 1)
    var = np.where(m, (x - mu) ** 2, 0).sum(axis=1, keepdims=True, dtype=np.float32) / np.maximum(cnt - 1, 1)
    return np.where(m, (x - mu) / (np.sqrt(var) + F32(1e-9)), np.nan).astype(F32)


def cs_demean(x):
    """Per-row ``x - mean`` of the finite values."""
    return (x - nanmean_row(x)[:, None]).astype(F32)


# ---------------------------------------------------------------------------------------------------------------
# language tables
# ---------------------------------------------------------------------------------------------------------------
def _safe_div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.abs(b) > EPS, a / b, np.nan)


def _unary_log(a):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log(np.abs(a) + F32(EPS))


def _sign(a):
    """+1, -1 or 0; NaN gives 0 (the old engine's torch.sign did, and nine frozen expressions depend on it)."""
    return (a > 0).astype(F32) - (a < 0).astype(F32)


UNARY: dict[str, Callable] = {
    "Abs": np.abs, "Log": _unary_log, "Sign": _sign, "Sqrt": lambda a: np.sqrt(np.abs(a)), "CSRank": cs_rank,
    "CSZScore": cs_zscore, "CSDemean": cs_demean, "Neg": lambda a: -a,
    "Inv": lambda a: np.where(np.abs(a) > EPS, 1 / np.where(np.abs(a) > EPS, a, 1), np.nan),
}
BINARY: dict[str, Callable] = {"Greater": np.maximum, "Less": np.minimum, "Add": np.add, "Sub": np.subtract,
                               "Mul": np.multiply, "Div": _safe_div}
ROLL: dict[str, Callable] = {
    "Ref": shift, "Mean": ts_mean, "Sum": ts_sum, "Std": ts_std, "Var": ts_var, "Max": ts_max, "Min": ts_min,
    "Med": ts_median, "Mad": ts_mad, "Delta": ts_delta, "WMA": ts_wma, "EMA": ts_ema, "Slope": ts_slope,
    "Rsquare": ts_rsquare, "Resi": ts_resi, "Rank": ts_rank, "IdxMax": ts_argmax, "IdxMin": ts_argmin,
}
PAIR: dict[str, Callable] = {"Corr": lambda a, b, d: ts_cov(a, b, d, corr=True), "Cov": ts_cov}
_NEEDS_TWO = ("Std", "Var", "Slope", "Rsquare", "Resi", "Rank", "Mad")

_TOKEN = re.compile(r"\s*(?:(\d+\.\d*|\d*\.\d+|\d+)(?:[eE]([-+]?\d+))?|(\$[a-z_]+)|([A-Za-z]+)|(.))")


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos, s = 0, text.strip()
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise ExprError(f"cannot read {s[pos:pos + 10]!r}")
        num, exp, field, ident, ch = m.groups()
        pos = m.end()
        if num is not None:
            out.append(("num", num + (f"e{exp}" if exp else "")))
        elif field:
            out.append(("field", field[1:]))
        elif ident:
            out.append(("id", ident))
        elif ch and ch.strip():
            out.append(("op", ch))
    return out


class _Parser:
    def __init__(self, text: str):
        self.toks, self.i = _tokens(text), 0

    def peek(self) -> tuple[str, str]:
        return self.toks[self.i] if self.i < len(self.toks) else ("end", "")

    def take(self, kind: str | None = None, val: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if (kind and tok[0] != kind) or (val and tok[1] != val):
            raise ExprError(f"expected {val or kind}, got {tok[1]!r}")
        self.i += 1
        return tok

    def parse(self) -> tuple:
        node = self.cmp()
        if self.peek()[0] != "end":
            raise ExprError(f"trailing {self.peek()[1]!r}")
        return node

    def cmp(self) -> tuple:
        a = self.add()
        if self.peek() in (("op", ">"), ("op", "<")):
            op = self.take()[1]
            return ("gt" if op == ">" else "lt", a, self.add())
        return a

    def add(self) -> tuple:
        a = self.mul()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            a = ("Add" if op == "+" else "Sub", a, self.mul())
        return a

    def mul(self) -> tuple:
        a = self.un()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            a = ("Mul" if op == "*" else "Div", a, self.un())
        return a

    def un(self) -> tuple:
        if self.peek() == ("op", "-"):
            self.take()
            return ("Neg", self.un())
        return self.atom()

    def atom(self) -> tuple:
        kind, val = self.peek()
        if kind == "num":
            self.take()
            return ("num", float(val))
        if kind == "field":
            self.take()
            if val not in FIELDS:
                raise ExprError(f"unknown field ${val}")
            return ("field", val)
        if kind == "op" and val == "(":
            self.take()
            node = self.cmp()
            self.take("op", ")")
            return node
        if kind == "id":
            self.take()
            self.take("op", "(")
            args = [self.cmp()]
            while self.peek() == ("op", ","):
                self.take()
                args.append(self.cmp())
            self.take("op", ")")
            return self._call(val, args)
        raise ExprError(f"unexpected {val!r}")

    @staticmethod
    def _window(node: tuple, name: str) -> int:
        if node[0] != "num" or node[1] != int(node[1]) or not 1 <= node[1] <= MAX_WINDOW:
            raise ExprError(f"{name} needs an integer window in 1..{MAX_WINDOW}")
        return int(node[1])

    def _call(self, name: str, args: list[tuple]) -> tuple:
        if name in UNARY and len(args) == 1:
            return (name, args[0])
        if name in BINARY and len(args) == 2:
            return (name, args[0], args[1])
        if name in ROLL and len(args) == 2:
            d = self._window(args[1], name)
            if name in _NEEDS_TWO and d < 2:
                raise ExprError(f"{name} needs a window >= 2")
            return (name, args[0], d)
        if name == "Quantile" and len(args) == 3 and args[2][0] == "num" and 0 < args[2][1] < 1:
            return ("Quantile", args[0], self._window(args[1], name), args[2][1])
        if name in PAIR and len(args) == 3:
            d = self._window(args[2], name)
            if d < 3:
                raise ExprError(f"{name} needs a window >= 3")
            return (name, args[0], args[1], d)
        raise ExprError(f"bad call {name} with {len(args)} args")


def parse(text: str) -> tuple:
    """Parse an expression into a tuple tree: ``("num", v)``, ``("field", name)`` or ``(Func, child..., window...)``."""
    return _Parser(text).parse()


def canon(node: tuple) -> str:
    """Canonical text of a tree (cache key); equal trees give equal text."""
    kind = node[0]
    if kind == "num":
        return repr(node[1])
    if kind == "field":
        return "$" + node[1]
    return f"{kind}({','.join(canon(a) if isinstance(a, tuple) else repr(a) for a in node[1:])})"


def fields_of(node: tuple) -> set[str]:
    """Names of the ``$fields`` an expression reads."""
    if node[0] == "field":
        return {node[1]}
    out: set[str] = set()
    for a in node[1:]:
        if isinstance(a, tuple):
            out |= fields_of(a)
    return out


class Evaluator:
    """Evaluates trees on a field mapping ``{name: [T, N] float32}`` with an LRU cache of shared subtrees."""

    def __init__(self, fields: Mapping[str, np.ndarray], cache_size: int = 32):
        self.f = fields
        any_grid = next(iter(fields.values()))
        self.shape = any_grid.shape
        self.cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self.cache_size = cache_size

    def __call__(self, expr: str | tuple) -> np.ndarray:
        node = parse(expr) if isinstance(expr, str) else expr
        out = self._eval(node)
        return np.where(np.isfinite(out), out, np.nan).astype(F32)

    def _eval(self, node: tuple) -> np.ndarray:
        kind = node[0]
        if kind == "num":
            return np.full(self.shape, node[1], dtype=F32)
        if kind == "field":
            return self.f[node[1]]
        key = canon(node)
        hit = self.cache.get(key)
        if hit is not None:
            self.cache.move_to_end(key)
            return hit
        a = [self._eval(x) if isinstance(x, tuple) else x for x in node[1:]]
        with np.errstate(all="ignore"):
            if kind in UNARY:
                out = UNARY[kind](a[0])
            elif kind in BINARY:
                out = BINARY[kind](a[0], a[1])
            elif kind in ("gt", "lt"):
                ok = np.isfinite(a[0]) & np.isfinite(a[1])
                out = np.where(ok, (a[0] > a[1]) if kind == "gt" else (a[0] < a[1]), np.nan)
            elif kind in ROLL:
                out = ROLL[kind](a[0], a[1])
            elif kind == "Quantile":
                out = ts_quantile(a[0], a[1], a[2])
            elif kind in PAIR:
                out = PAIR[kind](a[0], a[1], a[2])
            else:  # pragma: no cover - the parser only builds the kinds above
                raise ExprError(kind)
        out = np.asarray(out).astype(F32)
        self.cache[key] = out
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return out
