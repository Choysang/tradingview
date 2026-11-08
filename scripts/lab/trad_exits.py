"""Traditional rules re-tested with their own kind of exit (lab venv).

Round 5 held every rule for 5 days, which is unfair to a breakout system meant to ride a trend or a
打板 rule meant to sell the next day. Here each rule (75 from trad_signals.parquet plus the transferable
World Cup Trading Championship rules below) is run with seven exit families on the event level:
  fix1     sell at the open after the entry day (shortest legal hold under T+1);
  fix5     the round-5 baseline;
  fix20    hold 20 days;
  stop8    close 8% below entry -> sell at the next open; at most 10 days;
  sl5tp10  stop 5%, take profit 10% (both on the close); at most 10 days;
  trail10  close 10% below the highest close since entry; at most 60 days (trend following);
  ma20     close below the 20-day average; at most 60 days.
Entry at the next open (skipped when that open is sealed at the limit). Return net of 0.2% round trip,
excess over the equal-weight market's open-to-open return over the same days. Each rule's exit family is
chosen on PRE events whose exit falls inside PRE (no peeking into July); the chosen version is then
reported on SELECT and VERIFY with day-clustered Newey-West t and one Benjamini-Hochberg pass over rules.
Approximations: a sealed limit-down open does not block the exit; events with a missing price on the
exit day are dropped.
WCTC transferable rules (Unger, Williams, Dennis-style, from the round-5 survey):
  wctc_breakout20  close above the prior 20-day high close with volume above its 20-day mean;
  wctc_outside_up  bullish outside bar: high above and low below yesterday's, close above yesterday's high;
  wctc_nr7_up      narrowest range of 7 days, then a close above that bar's high the next day.
Pre-holiday effect is a market-level timing claim and is printed separately.
Output .work/lab/trad_exits.json.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np
import polars as pl
import torch

import tengine as E

COST = 0.002
FAMILIES = {"fix1": dict(H=1), "fix5": dict(H=5), "fix20": dict(H=20), "stop8": dict(H=10, stop=0.08),
            "sl5tp10": dict(H=10, stop=0.05, tp=0.10), "trail10": dict(H=60, trail=0.10), "ma20": dict(H=60, ma=True)}
HMAX = 60
PRE_END, SELECT_END = E.PRE_END, E.SELECT_END


def wctc_rules(d: E.Data) -> dict[str, torch.Tensor]:
    f = d.f
    c, h, l, v = f["close"], f["high"], f["low"], f["volume"]
    out = {"wctc_breakout20": (c > E._shift(E.ts_max(c, 20), 1)) & (v > E.ts_mean(v, 20))}
    out["wctc_outside_up"] = (h > E._shift(h, 1)) & (l < E._shift(l, 1)) & (c > E._shift(h, 1))
    rng = h - l
    nr7 = rng <= E.ts_min(rng, 7)
    out["wctc_nr7_up"] = E._shift(nr7.to(torch.float32), 1).nan_to_num(0) > 0
    out["wctc_nr7_up"] &= c > E._shift(h, 1)
    return {k: torch.nan_to_num(x.to(torch.float32), nan=0.0) > 0 for k, x in out.items()}


def market_log(d: E.Data) -> torch.Tensor:
    o = d.f["open"]
    r = torch.where(d.tradable, E._shift(o, -1) / o - 1, torch.nan)
    m = torch.nan_to_num(E._nanmean_row(r), nan=0.0).double()
    return torch.cat([torch.zeros(1, dtype=torch.float64), torch.cumsum(torch.log1p(m), 0)])  # L[t] = sum_{s<t}


class Paths:
    def __init__(self, d: E.Data):
        self.d = d
        f = d.f
        self.o, self.c = f["open"], f["close"]
        self.ma = E.ts_mean(self.c, 20)
        self.blocked = f["open_at_limit"] > 0
        self.L = market_log(d)

    def run(self, ti: torch.Tensor, ni: torch.Tensor) -> dict[str, dict[str, np.ndarray]]:
        d = self.d
        ent = ti + 1
        keep = ent < d.T
        ti, ni, ent = ti[keep], ni[keep], ent[keep]
        ok = ~self.blocked[ent, ni] & torch.isfinite(self.o[ent, ni])
        ti, ni, ent = ti[ok], ni[ok], ent[ok]
        k = torch.arange(HMAX + 1)
        rows = (ent[:, None] + k[None, :]).clamp(max=d.T - 1)
        inside = (ent[:, None] + k[None, :]) < d.T
        po, pc, pm = self.o[rows, ni[:, None]], self.c[rows, ni[:, None]], self.ma[rows, ni[:, None]]
        po, pc = torch.where(inside, po, torch.nan), torch.where(inside, pc, torch.nan)
        e = po[:, 0]
        out = {}
        for name, fam in FAMILIES.items():
            H = fam["H"]
            cond = torch.zeros((len(e), H), dtype=torch.bool)
            rc = pc[:, :H] / e[:, None] - 1
            if "stop" in fam:
                cond |= rc <= -fam["stop"]
            if "tp" in fam:
                cond |= rc >= fam["tp"]
            if "trail" in fam:
                peak = torch.cummax(torch.nan_to_num(pc[:, :H], nan=-1.0), 1).values
                cond |= pc[:, :H] / peak - 1 <= -fam["trail"]
            if fam.get("ma"):
                cond |= pc[:, :H] < pm[:, :H]
            first = torch.where(cond.any(1), cond.to(torch.int8).argmax(1), torch.full((len(e),), H - 1))
            kx = torch.where(cond.any(1), first + 1, torch.full((len(e),), H))  # exit at the open of day kx
            px = po[torch.arange(len(e)), kx]
            xday = ent + kx
            good = torch.isfinite(px) & (xday < d.T)
            ret = px / e - 1 - COST
            mkt = torch.exp(self.L[xday.clamp(max=d.T)] - self.L[ent]) - 1
            out[name] = {"t": ti[good].numpy(), "xday": xday[good].numpy(), "ret": ret[good].double().numpy() * 100,
                         "ex": (ret[good].double() - mkt[good]).numpy() * 100, "days": kx[good].numpy()}
        return out


def day_t(t: np.ndarray, x: np.ndarray, lag: int) -> tuple[float, float, int]:
    if len(x) < 10:
        return float("nan"), float("nan"), 0
    df = pl.DataFrame({"t": t, "x": x}).group_by("t").agg(pl.col("x").mean()).sort("t")
    v = df["x"].to_numpy()
    return float(v.mean()), E.nw_t(v, max(1, lag)), len(v)


def bh(p: list[float], q: float = 0.10) -> list[bool]:
    p = np.array(p)
    order = np.argsort(p)
    passed = np.zeros(len(p), dtype=bool)
    ok = p[order] <= q * (np.arange(1, len(p) + 1) / len(p))
    if ok.any():
        passed[order[: np.max(np.nonzero(ok)[0]) + 1]] = True
    return passed.tolist()


def pre_holiday(d: E.Data) -> dict:
    gaps = np.array([(d.dates[i + 1] - d.dates[i]).days for i in range(d.T - 1)] + [1])
    r = E._nanmean_row(torch.where(d.tradable, d.f["ret"], torch.nan)).numpy() * 100
    pre = gaps >= 4  # the last trading day before a break of 3+ calendar days off (long weekends and holidays)
    pre_long = gaps >= 5
    out = {"pre_break_days": int(pre.sum()), "pre_break_mean": float(np.nanmean(r[pre])), "other_mean": float(np.nanmean(r[~pre])),
           "pre_holiday_days": int(pre_long.sum()), "pre_holiday_mean": float(np.nanmean(r[pre_long]))}
    print(f"\n[pre-holiday] equal-weight market close-to-close: before a 3+ day break {out['pre_break_mean']:+.3f}% ({out['pre_break_days']} days), "
          f"before a 4+ day holiday {out['pre_holiday_mean']:+.3f}% ({out['pre_holiday_days']}), other days {out['other_mean']:+.3f}%")
    return out


def main() -> None:
    d = E.Data()
    P = Paths(d)
    col = {c: i for i, c in enumerate(d.codes)}
    di = {x: i for i, x in enumerate(d.dates)}
    sig = pl.read_parquet(E.LAB / "trad_signals.parquet")
    rules: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
    for (rule,), g in sig.group_by(["rule"], maintain_order=True):
        t = [di.get(x) for x in g["date"].to_list()]
        n = [col.get(x) for x in g["code"].to_list()]
        keep = [(a, b) for a, b in zip(t, n) if a is not None and b is not None]
        rules[rule] = (torch.tensor([a for a, _ in keep]), torch.tensor([b for _, b in keep]))
    for name, m in wctc_rules(d).items():
        m &= d.tradable
        rules[name] = torch.nonzero(m, as_tuple=True)
    pre_end = max(i for i, x in enumerate(d.dates) if x <= PRE_END)
    sel = (pre_end + 1, max(i for i, x in enumerate(d.dates) if x <= SELECT_END))
    ver = (sel[1] + 1, d.T - 1) if os.environ.get("VERIFY") == "1" else None
    res = {}
    for rule, (ti, ni) in rules.items():
        if len(ti) == 0:
            continue
        paths = P.run(ti, ni)
        cell = {}
        for fam, x in paths.items():
            pre = (x["t"] <= pre_end) & (x["xday"] <= pre_end)
            lag = int(np.median(x["days"])) if len(x["days"]) else 1
            r = {"PRE": day_t(x["t"][pre], x["ex"][pre], lag)}
            for pname, span in (("SELECT", sel), ("VERIFY", ver)):
                if span:
                    m = (x["t"] >= span[0]) & (x["t"] <= span[1])
                    r[pname] = day_t(x["t"][m], x["ex"][m], lag)
            r["hold_median"] = lag
            cell[fam] = r
        best = max(cell, key=lambda f: np.nan_to_num(cell[f]["PRE"][0], nan=-99))
        res[rule] = {"best": best, "families": cell}
    names = list(res)
    for pname in ("SELECT", "VERIFY"):
        if pname == "VERIFY" and not ver:
            continue
        tv = [res[n]["families"][res[n]["best"]][pname][1] for n in names]
        p = [float(1 - 0.5 * (1 + math.erf(t / np.sqrt(2)))) if np.isfinite(t) else 1.0 for t in tv]  # one-sided: excess > 0
        passed = bh(p)
        for n, ok, pv in zip(names, passed, p):
            res[n][f"{pname}_p"], res[n][f"{pname}_bh"] = pv, ok
        print(f"\n[{pname}] rules with their PRE-chosen exit: {sum(passed)} of {len(names)} pass BH q=0.10 (one-sided)")
    order = sorted(names, key=lambda n: -np.nan_to_num(res[n]["families"][res[n]["best"]]["PRE"][0], nan=-99))
    print("\nrule, PRE-chosen exit (median hold), mean excess per entry-day % (NW t) PRE | SELECT | VERIFY; fix5 baseline SELECT")
    for n in order:
        b = res[n]["best"]
        f = res[n]["families"][b]
        s = " | ".join(f"{k} {f[k][0]:+.2f} ({f[k][1]:+.1f})" for k in ("PRE", "SELECT", "VERIFY") if k in f)
        base = res[n]["families"]["fix5"]["SELECT"]
        print(f"  {n:24s} {b:8s}({f['hold_median']:2d}d) {s} | fix5 SELECT {base[0]:+.2f}")
    fam_count = {f: sum(res[n]["best"] == f for n in names) for f in FAMILIES}
    print("\nexit chosen on PRE:", fam_count)
    out = {"rules": res, "chosen": fam_count, "pre_holiday": pre_holiday(d)}
    json.dump(out, open(E.LAB / "trad_exits.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    sys.exit(main())
