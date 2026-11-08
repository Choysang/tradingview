"""Dense [days x stocks] tensor engine for factor research (torch, CPU). Runs in the lab venv
(.work/labenv), reading the real panel exported by export_panel.py; no backend imports.

Expressions use Qlib's syntax, e.g. "Mean($close, 5)/$close" or "Corr($close, Log($volume+1), 10)",
parsed by a small recursive-descent parser (never eval). Values on row t use bars up to t only;
Ref(x, d) needs d >= 1. Missing bars are NaN; a rolling window needs at least 3/4 of its bars.

Labels: fwd[h][t] = open[t+1+h] / open[t+1] - 1 in percent, minus the mean over the day's
tradable stocks, NaN when the stock is not tradable at t or the next open is a sealed limit-up.

Universe: env LAB_UNIVERSE=main keeps only Shanghai and Shenzhen main-board stocks (no ChiNext, STAR or
Beijing), counts market sentiment only over stocks listed at least NEW_DAYS days (a new listing's
one-word run after the IPO is not market sentiment), uses the Shanghai Composite (上证指数) as the index,
and sends every output to .work/lab/main/ (inputs fall back to .work/lab/ via `inp`).
"""
from __future__ import annotations

import math
import os
import re
import shutil
from collections import OrderedDict
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import torch

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / ".work" / "lab"
UNIVERSE = os.environ.get("LAB_UNIVERSE", "all")
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")  # 沪深主板 (中小板 merged in 2021)
NEW_DAYS = 60
LAB = BASE / "main" if UNIVERSE == "main" else BASE
LAB.mkdir(parents=True, exist_ok=True)
# inputs shared by both universes (expressions, signals, maps); copied on first use so `E.LAB / name` reads work
SHARED = ("mined_pool.json", "mined_survivors.json", "rl_alphagen.json", "sectors.parquet", "sw_industry.parquet",
          "trad_signals.parquet", "gp_pre.jsonl", "gp_recent.jsonl", "llm_mine_pre.jsonl", "judge_cards.jsonl")
if LAB != BASE:
    for _n in SHARED:
        if (BASE / _n).exists() and (not (LAB / _n).exists() or (BASE / _n).stat().st_mtime > (LAB / _n).stat().st_mtime):
            shutil.copy2(BASE / _n, LAB / _n)
torch.set_num_threads(8)
F = torch.float32
NAN = float("nan")
EPS = 1e-12
PRE_END, SELECT_START, SELECT_END, VERIFY_START = date(2026, 6, 30), date(2026, 7, 1), date(2026, 8, 31), date(2026, 9, 1)


def in_universe(codes: pl.Expr) -> pl.Expr:
    return codes.str.slice(0, 3).is_in(MAIN_PREFIX) if UNIVERSE == "main" else pl.lit(True)


class Data:
    def __init__(self, path: Path = BASE / "panel.parquet"):
        p = pl.read_parquet(path)
        p = p.filter(in_universe(pl.col("code")))
        if "st_like" in p.columns:  # price-based ST flag (names in the panel are today's, so not used)
            p = p.with_columns(pl.col("st_like").cast(pl.Float32).alias("_st"))
        self.dates = sorted(p["date"].unique().to_list())
        self.codes = sorted(p["code"].unique().to_list())
        di = {d: i for i, d in enumerate(self.dates)}
        ci = {s: i for i, s in enumerate(self.codes)}
        self.T, self.N = len(self.dates), len(self.codes)
        r = np.array([di[d] for d in p["date"].to_list()])
        k = np.array([ci[s] for s in p["code"].to_list()])

        def grid(col: str, dtype=np.float32) -> torch.Tensor:
            a = np.full((self.T, self.N), np.nan, dtype=np.float32)
            a[r, k] = p[col].cast(pl.Float64).fill_null(np.nan).to_numpy().astype(np.float32)
            return torch.from_numpy(a)

        self.f = {x: grid(x) for x in ("open", "high", "low", "close", "volume", "amount", "prev_close", "pct_chg", "float_cap")}
        for x in ("is_limit_up", "is_limit_down", "touched_limit", "is_broken", "open_at_limit", "tradable"):
            g = grid(x)
            self.f[x] = g
        self.f["limit_streak"] = grid("limit_streak")
        self.f["days_listed"] = grid("days_listed")
        self.f["is_st"] = grid("_st") if "_st" in p.columns else torch.zeros((self.T, self.N))
        self.f["limit_ratio"] = grid("limit_ratio")
        for x in ("one_word", "lhb_on", "smart_buyers", "smart_score", "trip_buyers", "inst_net"):  # 龙虎榜 and 一字板, when exported
            if x in p.columns:
                self.f[x] = grid(x)
        f = self.f
        f["turn"] = f["amount"] / f["float_cap"] * 100
        f["vwap"] = (f["high"] + f["low"] + f["close"]) / 3  # amount/volume is unadjusted, so use the typical price
        f["ret"] = f["pct_chg"] / 100
        f["mkt"] = _broadcast(_nanmean_row(torch.where(f["tradable"] > 0, f["ret"], NAN)), self.N)
        f["cap"] = f["float_cap"]
        self.tradable = (f["tradable"] > 0) & torch.isfinite(f["close"])
        # stocks that count for market-wide sentiment (limit-up counts, highest streak, premium)
        self.live = torch.isfinite(f["close"]) & (((f["days_listed"] >= NEW_DAYS) & (f["is_st"] < 0.5)) if UNIVERSE == "main" else True)
        f["cgo"] = capital_gains_overhang(f["close"], f["turn"])
        f["tk"] = prospect_value(f["ret"], 60)
        self.date_index = di
        b = pl.read_parquet(BASE / ("benchmark_sh.parquet" if UNIVERSE == "main" else "benchmark.parquet")).sort("date")
        bmap = dict(zip(b["date"].to_list(), b["close"].to_list()))
        self.index_close = torch.tensor([bmap.get(d, NAN) for d in self.dates], dtype=torch.float64)
        self._labels: dict[int, torch.Tensor] = {}
        self.industry = None
        ind = p.select("code", "industry").drop_nulls().unique("code", keep="last")
        self.industry_of = dict(zip(ind["code"].to_list(), ind["industry"].to_list()))

    def label(self, h: int) -> torch.Tensor:
        if h not in self._labels:
            o = self.f["open"]
            nxt = _shift(o, -1)
            fwd = (_shift(o, -1 - h) / nxt - 1) * 100
            blocked = _shift(self.f["open_at_limit"], -1) > 0
            fwd = torch.where(self.tradable & torch.isfinite(fwd) & ~blocked, fwd, NAN)
            self._labels[h] = fwd - _nanmean_row(fwd)[:, None]
        return self._labels[h]

    def rows(self, start: date, end: date) -> slice:
        idx = [i for i, d in enumerate(self.dates) if start <= d <= end]
        return slice(idx[0], idx[-1] + 1)


# --- behavioural fields -----------------------------------------------------------------------


def capital_gains_overhang(close: torch.Tensor, turn_pct: torch.Tensor) -> torch.Tensor:
    """Grinblatt & Han (2005) disposition-effect proxy: the turnover-weighted reference (cost) price
    RP_t = v_t * P_t + (1 - v_t) * RP_{t-1} with v = daily turnover as a fraction; CGO = P / RP - 1.
    Undefined for the first 60 bars of a stock (the reference price needs time to form)."""
    v = torch.nan_to_num(turn_pct / 100, nan=0.0).clamp(0, 1)
    rp = torch.full_like(close, NAN)
    prev = torch.full((close.shape[1],), NAN)
    age = torch.zeros(close.shape[1])
    for t in range(close.shape[0]):
        p = close[t]
        ok = torch.isfinite(p)
        new = torch.where(torch.isfinite(prev), v[t] * p + (1 - v[t]) * prev, p)
        prev = torch.where(ok, new, prev)
        age = age + ok.to(F)
        rp[t] = torch.where(age >= 60, prev, NAN)
    return close / rp - 1


def prospect_value(ret: torch.Tensor, d: int) -> torch.Tensor:
    """Barberis, Mukherjee & Wang (2016) prospect-theory value of the last d daily returns: Tversky-
    Kahneman value function (alpha 0.88, lambda 2.25) with cumulative probability weighting (0.61 gains,
    0.69 losses), each return treated as an equally likely outcome."""
    a, lam, dg, dl = 0.88, 2.25, 0.61, 0.69

    def w(p, dlt):
        return p ** dlt / (p ** dlt + (1 - p) ** dlt) ** (1 / dlt)

    out = torch.full_like(ret, NAN)
    if d > ret.shape[0]:
        return out
    for cols, win in _windows(ret, d, chunk=600):
        s, _ = torch.sort(torch.nan_to_num(win, nan=0.0), dim=-1)  # ascending
        n = torch.isfinite(win).sum(-1)
        i = torch.arange(1, d + 1, dtype=F)
        # losses: outcome i (ascending) has decision weight w(i/d) - w((i-1)/d); gains mirror from the top
        wl = w(i / d, dl) - w((i - 1) / d, dl)
        j = d - i + 1
        wg = w(j / d, dg) - w((j - 1) / d, dg)
        val = torch.where(s < 0, -lam * (-s).clamp(min=0) ** a * wl, s.clamp(min=0) ** a * wg)
        out[d - 1:, cols] = torch.where(n >= _minp(d), val.sum(-1), NAN)
    return out


# --- tensor helpers ---------------------------------------------------------------------------


def _broadcast(v: torch.Tensor, n: int) -> torch.Tensor:
    return v[:, None].expand(-1, n).contiguous()


def _nanmean_row(x: torch.Tensor) -> torch.Tensor:
    m = torch.isfinite(x)
    return torch.where(m, x, 0).sum(1) / m.sum(1).clamp(min=1)


def _shift(x: torch.Tensor, d: int) -> torch.Tensor:
    """d > 0: value from d rows earlier; d < 0: from |d| rows later (labels only)."""
    out = torch.full_like(x, NAN)
    if d > 0:
        out[d:] = x[:-d]
    elif d < 0:
        out[:d] = x[-d:]
    else:
        out = x.clone()
    return out


def _minp(d: int) -> int:
    return max(1, math.ceil(0.75 * d))


def _wsum(x: torch.Tensor, d: int) -> tuple[torch.Tensor, torch.Tensor]:
    """Rolling sum of finite values and their count over the last d rows (float64 inside)."""
    m = torch.isfinite(x)
    v = torch.where(m, x, 0).double()
    z = torch.zeros(1, x.shape[1], dtype=torch.float64)
    cs = torch.cat([z, v.cumsum(0)])
    cm = torch.cat([z, m.double().cumsum(0)])
    s = torch.full((x.shape[0], x.shape[1]), NAN, dtype=torch.float64)
    n = torch.zeros_like(s)
    if d <= x.shape[0]:
        s[d - 1:] = cs[d:] - cs[:-d]
        n[d - 1:] = cm[d:] - cm[:-d]
    return s, n


def ts_sum(x, d):
    s, n = _wsum(x, d)
    return torch.where(n >= _minp(d), s, NAN).to(F)


def ts_mean(x, d):
    s, n = _wsum(x, d)
    return torch.where(n >= _minp(d), s / n.clamp(min=1), NAN).to(F)


def ts_var(x, d):
    s, n = _wsum(x, d)
    s2, _ = _wsum(x.double() ** 2 if x.dtype != torch.float64 else x ** 2, d)
    var = (s2 - s * s / n.clamp(min=1)) / (n - 1).clamp(min=1)
    return torch.where(n >= max(2, _minp(d)), var.clamp(min=0), NAN).to(F)


def ts_std(x, d):
    return ts_var(x, d).sqrt()


def ts_cov(x, y, d, corr=False):
    m = torch.isfinite(x) & torch.isfinite(y)
    xx, yy = torch.where(m, x, NAN), torch.where(m, y, NAN)
    sx, n = _wsum(xx, d)
    sy, _ = _wsum(yy, d)
    sxy, _ = _wsum(xx.double() * yy.double(), d)
    cov = (sxy - sx * sy / n.clamp(min=1)) / (n - 1).clamp(min=1)
    ok = n >= max(3, _minp(d))
    if not corr:
        return torch.where(ok, cov, NAN).to(F)
    sx2, _ = _wsum(xx.double() ** 2, d)
    sy2, _ = _wsum(yy.double() ** 2, d)
    vx = (sx2 - sx * sx / n.clamp(min=1)) / (n - 1).clamp(min=1)
    vy = (sy2 - sy * sy / n.clamp(min=1)) / (n - 1).clamp(min=1)
    den = (vx * vy).clamp(min=0).sqrt()
    return torch.where(ok & (den > 1e-12 * (1 + vx.abs() + vy.abs())), cov / den, NAN).clamp(-1, 1).to(F)


def _regress(x, d):
    """Slope, intercept-at-last-point and R^2 of x on time over the last d rows (full windows only)."""
    T = x.shape[0]
    t = torch.arange(T, dtype=torch.float64)[:, None].expand(-1, x.shape[1])
    m = torch.isfinite(x)
    sx, n = _wsum(x, d)
    stx, _ = _wsum(torch.where(m, t * x.double(), NAN), d)
    sx2, _ = _wsum(x.double() ** 2, d)
    start = t - (d - 1)
    sl_x = stx - start * sx  # sum of local index * x
    sl, sl2 = d * (d - 1) / 2, (d - 1) * d * (2 * d - 1) / 6
    den = d * sl2 - sl * sl
    slope = (d * sl_x - sl * sx) / den
    mean_x, mean_l = sx / d, sl / d
    intercept = mean_x - slope * mean_l
    fit_last = intercept + slope * (d - 1)
    var_x = sx2 / d - mean_x ** 2
    var_l = sl2 / d - mean_l ** 2
    r2 = torch.where(var_x > 1e-18, (slope ** 2 * var_l / var_x).clamp(0, 1), NAN)
    full = n >= d
    return [torch.where(full, v, NAN) for v in (slope, fit_last, r2)]


def ts_slope(x, d):
    return _regress(x, d)[0].to(F)


def ts_rsquare(x, d):
    return _regress(x, d)[2].to(F)


def ts_resi(x, d):
    return (x.double() - _regress(x, d)[1]).to(F)


def _windows(x, d, chunk=800):
    """Yield (column slice, window tensor [T-d+1, n, d]) in column chunks to bound memory."""
    for j in range(0, x.shape[1], chunk):
        yield slice(j, j + chunk), x[:, j:j + chunk].unfold(0, d, 1)


def _win_reduce(x, d, fn):
    out = torch.full_like(x, NAN)
    if d > x.shape[0]:
        return out
    for cols, w in _windows(x, d):
        ok = torch.isfinite(w).sum(-1) >= _minp(d)
        out[d - 1:, cols] = torch.where(ok, fn(w), NAN)
    return out


def _valid_window(x, d):
    _, n = _wsum(x, d)
    return n >= _minp(d)


def _extreme(x, d, largest: bool, want_pos: bool):
    """Rolling max/min (and its 1-based position, oldest bar = 1) by looping over lags, oldest first,
    so ties keep the oldest bar like numpy's argmax."""
    fill = -torch.inf if largest else torch.inf
    xf = torch.where(torch.isfinite(x), x, fill)
    T = x.shape[0]
    best = torch.full_like(xf, fill)
    pos = torch.zeros_like(xf)
    for i in range(d - 1, -1, -1):  # i = lag; position in window = d - i
        cand = torch.full_like(xf, fill)
        cand[i:] = xf[: T - i]
        better = cand > best if largest else cand < best
        best = torch.where(better, cand, best)
        if want_pos:
            pos = torch.where(better, torch.tensor(float(d - i)), pos)
    ok = _valid_window(x, d)
    return torch.where(ok, pos if want_pos else best, NAN)


def ts_max(x, d):
    return _extreme(x, d, True, False)


def ts_min(x, d):
    return _extreme(x, d, False, False)


def ts_argmax(x, d):  # 1-based position of the max in the window, oldest bar = 1 (Qlib's IdxMax)
    return _extreme(x, d, True, True)


def ts_argmin(x, d):
    return _extreme(x, d, False, True)


def ts_quantile(x, d, q):
    return _win_reduce(x, d, lambda w: torch.nanquantile(w, q, dim=-1))


def ts_median(x, d):
    return ts_quantile(x, d, 0.5)


def ts_mad(x, d):  # mean absolute deviation around the window mean
    return _win_reduce(x, d, lambda w: (w - w.nanmean(-1, keepdim=True)).abs().nanmean(-1))


def ts_rank(x, d):  # percentile of today's value within its window (ties count half)
    T = x.shape[0]
    less = torch.zeros_like(x)
    eq = torch.zeros_like(x)
    cnt = torch.zeros_like(x)
    for i in range(d):
        past = x[: T - i]
        cur = x[i:]
        less[i:] += (past < cur).to(F)
        eq[i:] += (past == cur).to(F)
        cnt[i:] += torch.isfinite(past).to(F)
    ok = _valid_window(x, d) & torch.isfinite(x)
    return torch.where(ok, (less + 0.5 * eq) / cnt.clamp(min=1), NAN)


def ts_wma(x, d):  # linear weights, the newest bar weighs d
    T = x.shape[0]
    m = torch.isfinite(x)
    xv = torch.where(m, x, 0)
    out = torch.zeros_like(x)
    wsum = torch.zeros_like(x)
    for i in range(d):
        w = float(d - i)
        out[i:] += xv[: T - i] * w
        wsum[i:] += m[: T - i].to(F) * w
    return torch.where(_valid_window(x, d), out / wsum.clamp(min=EPS), NAN)


def ts_ema(x, d):  # pandas ewm(span=d, adjust=False), skipping missing bars
    a = 2 / (d + 1)
    out = torch.full_like(x, NAN)
    prev = torch.full((x.shape[1],), NAN)
    for t in range(x.shape[0]):
        cur = x[t]
        prev = torch.where(torch.isfinite(cur), torch.where(torch.isfinite(prev), a * cur + (1 - a) * prev, cur), prev)
        out[t] = prev
    _, n = _wsum(x, d)
    return torch.where(n >= _minp(d), out, NAN)


def ts_delta(x, d):
    return x - _shift(x, d)


def cs_rank(x):
    m = torch.isfinite(x)
    filled = torch.where(m, x, torch.inf)
    idx = filled.argsort(1)
    r = torch.empty_like(filled)
    r.scatter_(1, idx, torch.arange(x.shape[1], dtype=F).expand(x.shape[0], -1).contiguous())
    cnt = m.sum(1, keepdim=True).to(F)
    return torch.where(m, (r + 0.5) / cnt.clamp(min=1), NAN)


def cs_zscore(x):
    m = torch.isfinite(x)
    mu = torch.where(m, x, 0).sum(1, keepdim=True) / m.sum(1, keepdim=True).clamp(min=1)
    var = torch.where(m, (x - mu) ** 2, 0).sum(1, keepdim=True) / (m.sum(1, keepdim=True) - 1).clamp(min=1)
    return torch.where(m, (x - mu) / (var.sqrt() + 1e-9), NAN)


def cs_demean(x):
    return x - _nanmean_row(x)[:, None]


# --- expression language ----------------------------------------------------------------------

UNARY = {"Abs": torch.abs, "Log": lambda a: torch.log(a.abs() + 1e-12), "Sign": torch.sign,
         "Sqrt": lambda a: a.abs().sqrt(), "CSRank": cs_rank, "CSZScore": cs_zscore, "CSDemean": cs_demean,
         "Neg": lambda a: -a, "Inv": lambda a: torch.where(a.abs() > 1e-12, 1 / a, NAN)}
BINARY = {"Greater": torch.maximum, "Less": torch.minimum, "Add": torch.add, "Sub": torch.sub, "Mul": torch.mul,
          "Div": lambda a, b: torch.where(b.abs() > 1e-12, a / b, NAN)}
ROLL = {"Ref": _shift, "Mean": ts_mean, "Sum": ts_sum, "Std": ts_std, "Var": ts_var, "Max": ts_max, "Min": ts_min,
        "Med": ts_median, "Mad": ts_mad, "Delta": ts_delta, "WMA": ts_wma, "EMA": ts_ema, "Slope": ts_slope,
        "Rsquare": ts_rsquare, "Resi": ts_resi, "Rank": ts_rank, "IdxMax": ts_argmax, "IdxMin": ts_argmin}
PAIR = {"Corr": lambda a, b, d: ts_cov(a, b, d, corr=True), "Cov": ts_cov}
FIELDS = ("open", "high", "low", "close", "volume", "amount", "vwap", "turn", "ret", "mkt", "cap", "pct_chg",
          "is_limit_up", "is_limit_down", "touched_limit", "is_broken", "limit_streak", "prev_close", "cgo", "tk")
MAX_WINDOW = 250
_TOKEN = re.compile(r"\s*(?:(\d+\.\d*|\d*\.\d+|\d+)(?:[eE]([-+]?\d+))?|(\$[a-z_]+)|([A-Za-z]+)|(.))")


class ExprError(ValueError):
    pass


def _tokens(s: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    s = s.strip()
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
    """expr := cmp; cmp := add (('>'|'<') add)?; add := mul (('+'|'-') mul)*; mul := un (('*'|'/') un)*;
    un := '-' un | atom; atom := num | $field | Func '(' args ')' | '(' expr ')'. Builds a tuple tree."""

    def __init__(self, s: str):
        self.t, self.i = _tokens(s), 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else ("end", "")

    def take(self, kind=None, val=None):
        tok = self.peek()
        if (kind and tok[0] != kind) or (val and tok[1] != val):
            raise ExprError(f"expected {val or kind}, got {tok[1]!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.cmp()
        if self.peek()[0] != "end":
            raise ExprError(f"trailing {self.peek()[1]!r}")
        return node

    def cmp(self):
        a = self.add()
        if self.peek() in (("op", ">"), ("op", "<")):
            op = self.take()[1]
            return ("gt" if op == ">" else "lt", a, self.add())
        return a

    def add(self):
        a = self.mul()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            a = ("Add" if op == "+" else "Sub", a, self.mul())
        return a

    def mul(self):
        a = self.un()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            a = ("Mul" if op == "*" else "Div", a, self.un())
        return a

    def un(self):
        if self.peek() == ("op", "-"):
            self.take()
            return ("Neg", self.un())
        return self.atom()

    def atom(self):
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
    def _int(node, name):
        if node[0] != "num" or node[1] != int(node[1]) or not 1 <= node[1] <= MAX_WINDOW:
            raise ExprError(f"{name} needs an integer window in 1..{MAX_WINDOW}")
        return int(node[1])

    def _call(self, name, args):
        if name in UNARY and len(args) == 1:
            return (name, args[0])
        if name in BINARY and len(args) == 2:
            return (name, args[0], args[1])
        if name in ROLL and len(args) == 2:
            d = self._int(args[1], name)
            if name in ("Std", "Var", "Slope", "Rsquare", "Resi", "Rank", "Mad") and d < 2:
                raise ExprError(f"{name} needs a window >= 2")
            return (name, args[0], d)
        if name == "Quantile" and len(args) == 3 and args[2][0] == "num" and 0 < args[2][1] < 1:
            return ("Quantile", args[0], self._int(args[1], name), args[2][1])
        if name in PAIR and len(args) == 3:
            d = self._int(args[2], name)
            if d < 3:
                raise ExprError(f"{name} needs a window >= 3")
            return (name, args[0], args[1], d)
        raise ExprError(f"bad call {name} with {len(args)} args")


def parse(s: str) -> tuple:
    return _Parser(s).parse()


def canon(node: tuple) -> str:
    kind = node[0]
    if kind == "num":
        return repr(node[1])
    if kind == "field":
        return "$" + node[1]
    parts = [canon(a) if isinstance(a, tuple) else repr(a) for a in node[1:]]
    return f"{kind}({','.join(parts)})"


def size(node: tuple) -> int:
    return 1 + sum(size(a) for a in node[1:] if isinstance(a, tuple))


def depth(node: tuple) -> int:
    return 1 + max((depth(a) for a in node[1:] if isinstance(a, tuple)), default=0)


def fields_of(node: tuple) -> set[str]:
    if node[0] == "field":
        return {node[1]}
    return set().union(*(fields_of(a) for a in node[1:] if isinstance(a, tuple))) if len(node) > 1 else set()


class Evaluator:
    """Evaluates expression trees on a Data object, caching shared subtrees (LRU by canonical text)."""

    def __init__(self, data: Data, cache_size: int = 96):
        self.d, self.cache, self.cache_size = data, OrderedDict(), cache_size

    def __call__(self, expr: str | tuple) -> torch.Tensor:
        node = parse(expr) if isinstance(expr, str) else expr
        out = self._eval(node)
        return torch.where(torch.isfinite(out), out, NAN)

    def _eval(self, node: tuple) -> torch.Tensor:
        kind = node[0]
        if kind == "num":
            return torch.full((self.d.T, self.d.N), node[1], dtype=F)
        if kind == "field":
            return self.d.f[node[1]]
        key = canon(node)
        hit = self.cache.get(key)
        if hit is not None:
            self.cache.move_to_end(key)
            return hit
        a = [self._eval(x) if isinstance(x, tuple) else x for x in node[1:]]
        if kind in UNARY:
            out = UNARY[kind](a[0])
        elif kind in BINARY:
            out = BINARY[kind](a[0], a[1])
        elif kind in ("gt", "lt"):
            ok = torch.isfinite(a[0]) & torch.isfinite(a[1])
            out = torch.where(ok, ((a[0] > a[1]) if kind == "gt" else (a[0] < a[1])).to(F), NAN)
        elif kind in ROLL:
            out = ROLL[kind](a[0], a[1])
        elif kind == "Quantile":
            out = ts_quantile(a[0], a[1], a[2])
        elif kind in PAIR:
            out = PAIR[kind](a[0], a[1], a[2])
        else:
            raise ExprError(kind)
        out = out.to(F)
        self.cache[key] = out
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return out


# --- look-ahead check for price levels --------------------------------------------------------

LEVEL_FIELDS = ("open", "high", "low", "close", "vwap", "prev_close", "amount")


class ScaledView:
    """The same data with each stock's price fields (and the amount, which the warehouse builds as
    volume x adjusted typical price) multiplied by a per-stock constant k in [0.5, 1], and the float cap by
    k_cap in [0.9, 1]. Forward adjustment rewrites past levels by the stock's later dividends and bonus
    shares, so a factor whose daily ranks move under this rescaling reads the future through price levels."""

    def __init__(self, data: Data, seed: int = 7):
        g = torch.Generator().manual_seed(seed)
        k = 0.5 + 0.5 * torch.rand(data.N, generator=g)
        k_cap = 0.9 + 0.1 * torch.rand(data.N, generator=g)
        self.T, self.N, self.tradable = data.T, data.N, data.tradable
        self.f = dict(data.f)
        for name in LEVEL_FIELDS:
            self.f[name] = data.f[name] * k[None, :]
        self.f["cap"] = data.f["cap"] * k_cap[None, :]


def level_safe(expr: str, data: Data, scaled: ScaledView, rows: list[int], cut: float = 0.98) -> tuple[bool, float]:
    """True when the factor's daily ranks survive the per-stock rescaling (mean rank correlation >= cut)."""
    a = Evaluator(data, cache_size=4)(expr)[rows]
    b = Evaluator(scaled, cache_size=4)(expr)[rows]  # type: ignore[arg-type]
    rho = float(np.nanmean(daily_ic(a, b, min_n=50).numpy()))
    return rho >= cut, rho


# --- evaluation -------------------------------------------------------------------------------


class RankedLabel:
    """A label with its per-day ranks computed once, for fast approximate Spearman ICs: x is ranked
    over the rows where both are finite, y keeps its ranks over its own rows (coverage is ~99%,
    so the difference is negligible). `rows` restricts the days scored."""

    def __init__(self, y: torch.Tensor, rows: slice | list | None = None):
        self.rows = rows if rows is not None else slice(None)
        self.y = y[self.rows]
        self.ry = cs_rank(self.y)

    def ic(self, x: torch.Tensor, min_n: int = 100) -> torch.Tensor:
        xs = x[self.rows]
        m = torch.isfinite(xs) & torch.isfinite(self.y)
        rx = cs_rank(torch.where(m, xs, NAN))
        return _pearson_rows(rx, self.ry, m, min_n)


def _pearson_rows(rx, ry, m, min_n):
    rx, ry = torch.where(m, rx, 0), torch.where(m, ry, 0)
    n = m.sum(1).to(F)
    mx, my = rx.sum(1) / n.clamp(min=1), ry.sum(1) / n.clamp(min=1)
    cov = (rx * ry).sum(1) / n.clamp(min=1) - mx * my
    vx = (rx * rx).sum(1) / n.clamp(min=1) - mx * mx
    vy = (ry * ry).sum(1) / n.clamp(min=1) - my * my
    ic = cov / (vx * vy).clamp(min=1e-18).sqrt()
    return torch.where(n >= min_n, ic, NAN)


def daily_ic(x: torch.Tensor, y: torch.Tensor, min_n: int = 100) -> torch.Tensor:
    """Per-day Spearman rank IC between factor x and label y over rows where both are finite."""
    m = torch.isfinite(x) & torch.isfinite(y)
    rx, ry = cs_rank(torch.where(m, x, NAN)), cs_rank(torch.where(m, y, NAN))
    rx, ry = torch.where(m, rx - 0.5, 0), torch.where(m, ry - 0.5, 0)
    n = m.sum(1).to(F)
    mx, my = rx.sum(1) / n.clamp(min=1), ry.sum(1) / n.clamp(min=1)
    cov = (rx * ry).sum(1) / n.clamp(min=1) - mx * my
    vx = (rx * rx).sum(1) / n.clamp(min=1) - mx * mx
    vy = (ry * ry).sum(1) / n.clamp(min=1) - my * my
    ic = cov / (vx * vy).clamp(min=1e-18).sqrt()
    return torch.where(n >= min_n, ic, NAN)


def nw_t(v: np.ndarray, lag: int) -> float:
    v = v[np.isfinite(v)]
    n = len(v)
    if n < 8:
        return NAN
    d = v - v.mean()
    var = d @ d / n
    for k in range(1, min(lag, n - 1) + 1):
        var += 2 * (1 - k / (lag + 1)) * (d[k:] @ d[:-k]) / n
    return float(v.mean() / math.sqrt(var / n)) if var > 0 else NAN


def ic_stats(ic: torch.Tensor, rows: slice, lag: int) -> dict:
    v = ic[rows].double().numpy()
    v = v[np.isfinite(v)]
    if len(v) < 8:
        return {"ic": NAN, "t": NAN, "icir": NAN, "n": len(v)}
    return {"ic": float(v.mean()), "t": nw_t(v, lag), "icir": float(v.mean() / v.std(ddof=1)) if v.std() > 0 else NAN, "n": len(v)}


def coverage(x: torch.Tensor, data: Data) -> float:
    m = data.tradable
    return float((torch.isfinite(x) & m).sum() / m.sum().clamp(min=1))


def corr_with(x: torch.Tensor, others: list[torch.Tensor], rows: slice, sample: int = 40) -> list[float]:
    """Mean daily rank correlation of x with each other factor, on `sample` evenly spaced days."""
    idx = list(range(rows.start, rows.stop))
    idx = idx[:: max(1, len(idx) // sample)]
    rx = cs_rank(x[idx])
    out = []
    for o in others:
        out.append(float(np.nanmean(daily_ic(rx, cs_rank(o[idx]), min_n=50).numpy())))
    return out


def top_spread(x: torch.Tensor, y: torch.Tensor, rows: slice, q: float = 0.1) -> float:
    """Mean label of the top-q names by x minus the day's mean label (percent), averaged over days."""
    xs, ys = x[rows], y[rows]
    m = torch.isfinite(xs) & torch.isfinite(ys)
    r = cs_rank(torch.where(m, xs, NAN))
    top = m & (r >= 1 - q)
    val = torch.where(top, ys, 0).sum(1) / top.sum(1).clamp(min=1)
    ok = top.sum(1) >= 10
    return float(val[ok].mean()) if ok.any() else NAN
