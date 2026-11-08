"""Factor and strategy registry (lab venv): nothing ever generated is thrown away. Each expression keeps
its monthly IC history, so a factor that fades is shelved, not deleted, and can come back when its kind of
market returns.

Sources: the zoo (Alpha158 + extras + behavioral), the GP logs (gp_pre, gp_recent), the LLM miner log, the
AlphaGen pool and the mined pool; plus the strategy panel (75 rules + factor deciles, strategy_panel.parquet).
Each unique expression (canonical form) gets an id; its daily rank IC against the 5-day open-to-open excess
label is averaged per calendar month. A strategy's monthly value is its mean cohort 5-day excess.
Status (recomputed each run; the monthly history itself is append-only):
  active    long-run |t| >= 3 and the last 3 months agree with the long-run sign (>= 2 of 3 and the mean);
  dormant   long-run |t| >= 3 but the last 3 months do not agree;
  weak      long-run |t| < 3 (kept for the record and for the regimes where it worked);
  error     the expression could not be evaluated (the message is kept).
Regime conditioning: months carry a regime label (the leader-cut market period from cycles.json that covers
most of the month, else the 1-month cluster from regime_result.json); per factor, the signed
IC in each cluster; a factor whose best cluster is the latest month's cluster is flagged `fits_now`.
Analog test (does choosing by similar past markets work in advance?): for each month m from 2025-01, the 5
past months whose trailing-20-day market state is closest to the state just before m (labels complete
before m) pick the 20 factors with the best signed IC in those months (sign from the expanding history);
their realised signed IC in m is compared with all factors, last month's top 20, the expanding top 20 and a
random-analog null (500 draws).
Output .work/lab/registry/{factors,factor_months,strategies}.parquet and registry.json.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import sys
import time

import numpy as np
import polars as pl
import torch

import regime
import tengine as E
import zoo

OUT = E.LAB / "registry"
OUT.mkdir(exist_ok=True)
T_MIN = 3.0


def fid(canon: str) -> str:
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


def candidates() -> dict[str, dict]:
    """canonical expression -> {expr, sources, first_stat}; first source wins for the stored stat."""
    out: dict[str, dict] = {}

    def add(expr: str, source: str, stat: dict | None = None) -> None:
        try:
            key = E.canon(E.parse(expr))
        except Exception as exc:  # unparsable: kept under its raw text with the error
            key = expr
            stat = {**(stat or {}), "parse_error": repr(exc)[:200]}
        row = out.setdefault(key, {"expr": expr, "sources": [], "stat": stat or {}})
        if source not in row["sources"]:
            row["sources"].append(source)

    for name, expr in zoo.zoo().items():
        add(expr, f"zoo:{name}")
    for log in ("gp_pre", "gp_recent", "llm_mine_pre"):
        f = E.LAB / f"{log}.jsonl"
        if f.exists():
            for line in f.open(encoding="utf-8"):
                r = json.loads(line)
                if r.get("expr"):
                    add(r["expr"], log, {k: r.get(k) for k in ("ic", "t", "hypothesis", "name") if r.get(k) is not None})
    ag = E.LAB / "rl_alphagen.json"
    if ag.exists():
        for r in json.load(open(ag, encoding="utf-8"))["pool"]:
            add(r["expr"], "alphagen", {"ic": r.get("pre_ic")})
    mp = E.LAB / "mined_pool.json"
    if mp.exists():
        for r in json.load(open(mp, encoding="utf-8")):
            add(r["expr"], "mined_pool")
    return out


def month_index(d: E.Data) -> tuple[list[str], np.ndarray]:
    keys = [x.strftime("%Y-%m") for x in d.dates]
    months = sorted(set(keys))
    return months, np.array([months.index(k) for k in keys])


def monthly(v: np.ndarray, mi: np.ndarray, n: int) -> np.ndarray:
    out = np.full(n, np.nan)
    for m in range(n):
        x = v[mi == m]
        x = x[np.isfinite(x)]
        if len(x) >= 5:
            out[m] = x.mean()
    return out


def status(row: np.ndarray, last: int) -> tuple[str, float, float]:
    x = row[: last + 1]
    x = x[np.isfinite(x)]
    if len(x) < 6:
        return "weak", float("nan"), float("nan")
    t = float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if x.std() > 0 else 0.0
    tail = row[max(0, last - 2): last + 1]
    tail = tail[np.isfinite(tail)]
    s = np.sign(x.mean())
    if abs(t) < T_MIN:
        return "weak", t, float(tail.mean()) if len(tail) else float("nan")
    agree = len(tail) >= 2 and (np.sign(tail) == s).sum() >= 2 and np.sign(tail.mean()) == s
    return ("active" if agree else "dormant"), t, float(tail.mean())


def month_regimes(d: E.Data, months: list[str], mi: np.ndarray) -> tuple[np.ndarray, dict[int, str]]:
    """Each month's regime label. Leader-cut market periods from cycles.py when present (the period type that
    covers most of the month's days, e.g. 主线期·科技, 退潮·医药, 轮动期), else the 1-month k-means cluster.
    Round 7 (R7-3): the replay's day-by-day lines (replay_lines_v2.csv, Shenwan + concepts) come first: a day
    with a live line that reached 6+ boards is 主线期·<that theme>, a day with only shorter lines 轮动期, none
    无主线. A line's height is its final one, so the label describes the period; it is not a tradable signal."""
    lines_csv = E.BASE / "replay_lines_v2.csv"
    if lines_csv.exists():
        lines = pl.read_csv(lines_csv).with_columns(
            pl.col("start").str.to_date(), pl.when(pl.col("end") == "live").then(None).otherwise(pl.col("end")).str.to_date().alias("end"))
        dates = np.array(d.dates)
        day = np.array(["无主线" for _ in range(d.T)], dtype=object)
        best = np.zeros(d.T)
        for r in lines.iter_rows(named=True):
            on = (dates >= r["start"]) & (dates <= (r["end"] or dates[-1]))
            if r["max_height"] >= 6:
                upd = on & (best < r["max_height"])
                day[upd] = "主线期·" + r["theme"]
                best[upd] = r["max_height"]
            day[on & (best == 0)] = "轮动期"
        labels = []
        for m in range(len(months)):
            v = list(day[mi == m])
            labels.append(max(set(v), key=v.count) if v else "")
        names = sorted({x for x in labels if x})
        return np.array([names.index(x) if x else -1 for x in labels]), dict(enumerate(names))
    cyc = E.LAB / "cycles.json"
    if cyc.exists():
        day = np.array(["" for _ in range(d.T)], dtype=object)
        for c in json.load(open(cyc, encoding="utf-8"))["periods"]:
            day[c["start"]: c["end"]] = c["kind"] + ("·" + c["family"] if c.get("family") and c["kind"] != "轮动期" else "")
        labels = []
        for m in range(len(months)):
            v = [x for x in day[mi == m] if x]
            labels.append(max(set(v), key=v.count) if v else "")
        names = sorted({x for x in labels if x})
        return np.array([names.index(x) if x else -1 for x in labels]), dict(enumerate(names))
    rr = json.load(open(E.LAB / "regime_result.json", encoding="utf-8"))
    cl = {b: int(k) for b, k, _ in rr["1m"]["timeline"]}
    return np.array([cl.get(m, -1) for m in months]), {c["cluster"]: c["desc"] for c in rr["1m"]["clusters"]}


def analog(Mic: np.ndarray, months: list[str], state: np.ndarray, top: int = 20, K: int = 5, draws: int = 500) -> dict:
    rng = np.random.default_rng(5)
    start = months.index("2025-01")
    res = {"months": [], "analog": [], "all": [], "last_top": [], "hist_top": []}
    null = []
    for m in range(start, Mic.shape[1]):
        cand = [j for j in range(m - 1) if np.isfinite(state[j]).all()]  # month m-1's labels end inside m: purged
        if len(cand) < K + 3 or not np.isfinite(state[m]).all():
            continue
        sign = np.sign(np.nanmean(Mic[:, cand], axis=1))
        cur = Mic[:, m] * sign
        ok = np.isfinite(cur)
        mu, sd = state[cand].mean(0), state[cand].std(0) + 1e-12
        dist = (((state[cand] - mu) / sd - (state[m] - mu) / sd) ** 2).sum(1)
        near = [cand[i] for i in np.argsort(dist)[:K]]

        def pick(cols: list[int]) -> float:
            sc = np.nanmean(Mic[:, cols] * sign[:, None], axis=1)
            sc = np.where(ok, np.nan_to_num(sc, nan=-9), -99)
            return float(np.nanmean(cur[np.argsort(-sc)[:top]]))

        res["months"].append(months[m])
        res["analog"].append(pick(near))
        res["all"].append(float(np.nanmean(cur)))
        res["last_top"].append(pick([cand[-1]]))
        res["hist_top"].append(pick(cand))
        null.append([pick(list(rng.choice(cand, K, replace=False))) for _ in range(draws)])
    null = np.array(null)
    a = float(np.mean(res["analog"]))
    res["mean"] = {k: float(np.mean(res[k])) for k in ("analog", "all", "last_top", "hist_top")} | {"null": float(null.mean())}
    res["p_value"] = float((null.mean(0) >= a).mean())
    return res


def main() -> None:
    t0 = time.perf_counter()
    d = E.Data()
    months, mi = month_index(d)
    y = d.label(5)
    lab = E.RankedLabel(y)
    ev = E.Evaluator(d, cache_size=32)
    cands = candidates()
    print(f"[registry] {len(cands)} unique expressions, {len(months)} months", flush=True)
    rows, Mic = [], []
    cache = OUT / "_ic_cache.pkl"  # the IC loop takes ~40 min: a crash in the summary step must not redo it
    if cache.exists():
        saved = pickle.loads(cache.read_bytes())
        if saved["keys"] == list(cands) and saved["months"] == months:
            rows, Mic = saved["rows"], saved["mic"]
            print(f"[registry] IC matrix from {cache.name}", flush=True)
    for i, (key, c) in enumerate(cands.items() if not rows else ()):
        err = c["stat"].get("parse_error")
        mic = np.full(len(months), np.nan)
        if not err:
            try:
                mic = monthly(lab.ic(ev(key)).double().numpy(), mi, len(months))
            except Exception as exc:
                err = repr(exc)[:200]
        rows.append({"fid": fid(key), "expr": c["expr"], "canon": key, "sources": c["sources"], "error": err,
                     "mined_ic": c["stat"].get("ic"), "hypothesis": c["stat"].get("hypothesis")})
        Mic.append(mic)
        if i % 250 == 0:
            print(f"  [{i}/{len(cands)}] {time.perf_counter() - t0:.0f}s", flush=True)
    if not cache.exists() or pickle.loads(cache.read_bytes())["keys"] != list(cands):
        cache.write_bytes(pickle.dumps({"keys": list(cands), "months": months, "rows": rows, "mic": Mic}))
    Mic = np.array(Mic)
    # the latest month whose 5-day labels are complete (labels need 6 more bars)
    last = int(mi[d.T - 7])
    mcl, desc = month_regimes(d, months, mi)
    now = int(mcl[last])
    for r, row in zip(rows, Mic):
        st, t, tail = status(row, last) if r["error"] is None else ("error", float("nan"), float("nan"))
        s = np.sign(np.nanmean(row[: last + 1])) if np.isfinite(row[: last + 1]).any() else 0.0
        by = {}
        for k in sorted(set(mcl[mcl >= 0].tolist())):
            x = row[(mcl == k) & (np.arange(len(months)) <= last)] * s
            x = x[np.isfinite(x)]
            if len(x) >= 2:
                by[k] = float(x.mean())
        best = max(by, key=by.get) if by else None
        r |= {"status": st, "t": t, "sign": float(s), "ic_all": float(np.nanmean(row[: last + 1])) if s else None, "tail3": tail,
              "best_cluster": best, "best_cluster_ic": by.get(best) if best is not None else None,
              "cluster_ic": json.dumps({str(k): round(v, 4) for k, v in by.items()}),
              "fits_now": bool(best == now and st != "error"),
              "reactivate_when": f"regime {best}: {desc.get(best, '')}" if best is not None else None}
    fac = pl.DataFrame(rows, infer_schema_length=None)
    fac.write_parquet(OUT / "factors.parquet")
    long = pl.DataFrame({"fid": np.repeat([r["fid"] for r in rows], len(months)), "month": months * len(rows),
                         "ic": Mic.reshape(-1), "cluster": np.tile(mcl, len(rows))}).drop_nans("ic")
    long.write_parquet(OUT / "factor_months.parquet")
    # strategies: monthly mean cohort excess, same status rule
    panel = pl.read_parquet(E.LAB / "strategy_panel.parquet")
    snames, X = regime.strategy_month(panel, d)
    srows = []
    for n, x in zip(snames, X):
        mx = monthly(x, mi, len(months))
        st, t, tail = status(mx, last)
        srows.append({"strategy": n, "status": st, "t": t, "tail3": tail, "mean": float(np.nanmean(mx[: last + 1])),
                      **{f"m_{m}": float(v) for m, v in zip(months, mx)}})
    pl.DataFrame(srows, infer_schema_length=None).write_parquet(OUT / "strategies.parquet")
    # market state before each month (trailing 20 trading days) for the analog test
    df, ind_names = regime.daily_frame(d)
    first = [int(np.argmax(mi == m)) for m in range(len(months))]
    states = [regime.window_features(df, max(0, a - 20), a, ind_names) if a >= 20 else None for a in first]
    keys = [k for k in next(s for s in states if s) if not k.startswith("_")]
    S = np.array([[s[k] for k in keys] if s else [np.nan] * len(keys) for s in states])
    good = np.array([r["error"] is None for r in rows])
    res = analog(Mic[good][:, : last + 1], months[: last + 1], S[: last + 1])
    counts = fac.group_by("status").len().sort("status")
    summary = {"expressions": len(rows), "months": months, "last_complete_month": months[last], "cluster_now": now,
               "cluster_now_desc": desc.get(now), "status_counts": dict(counts.iter_rows()),
               "fits_now_dormant": fac.filter(pl.col("fits_now") & (pl.col("status") == "dormant")).height,
               "strategy_status": dict(pl.DataFrame(srows, infer_schema_length=None).group_by("status").len().iter_rows()), "analog": res}
    json.dump(summary, open(OUT / "registry.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)
    print(f"[registry] status {summary['status_counts']}; strategies {summary['strategy_status']}; cluster now {now} ({desc.get(now)})")
    m = res["mean"]
    print(f"[analog factors] {res['months'][0]}..{res['months'][-1]} ({len(res['months'])} months), signed IC of top 20: "
          f"analog {m['analog']:+.4f} | all {m['all']:+.4f} | last-month top {m['last_top']:+.4f} | expanding top {m['hist_top']:+.4f} "
          f"| random-analog null {m['null']:+.4f}, p = {res['p_value']:.3f}")
    print("  per month analog − all:", " ".join(f"{a}:{b - c:+.3f}" for a, b, c in zip(res["months"], res["analog"], res["all"])))
    print(f"done {time.perf_counter() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
