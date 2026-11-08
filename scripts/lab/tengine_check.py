"""Numerical checks of tengine against plain numpy on real stocks (run in the lab venv)."""
import time

import numpy as np
import polars as pl

import tengine as E

t0 = time.perf_counter()
d = E.Data()
print(f"data T={d.T} N={d.N} {time.perf_counter() - t0:.1f}s; tradable cells {int(d.tradable.sum())}")
ev = E.Evaluator(d)
p = pl.read_parquet(E.LAB / "panel.parquet").filter(pl.col("code").is_in(["600519", "000001", "300750"])).sort("code", "date")
rng = np.random.default_rng(0)

for code in ("600519", "000001", "300750"):
    j = d.codes.index(code)
    s = p.filter(pl.col("code") == code)
    rows = [d.date_index[x] for x in s["date"].to_list()]
    close = s["close"].to_numpy()
    vol = s["volume"].to_numpy().astype(float)
    checks = {
        "Mean($close,5)": np.convolve(close, np.ones(5) / 5, "full")[: len(close)],
        "Ref($close,3)": np.r_[[np.nan] * 3, close[:-3]],
        "Max($close,10)": np.array([close[max(0, i - 9): i + 1].max() for i in range(len(close))]),
        "Std($close,20)": np.array([close[i - 19: i + 1].std(ddof=1) if i >= 19 else np.nan for i in range(len(close))]),
        "Corr($close,Log($volume+1),10)": np.array([np.corrcoef(close[i - 9: i + 1], np.log(vol[i - 9: i + 1] + 1))[0, 1] if i >= 9 else np.nan for i in range(len(close))]),
        "Slope($close,10)": np.array([np.polyfit(np.arange(10), close[i - 9: i + 1], 1)[0] if i >= 9 else np.nan for i in range(len(close))]),
        "Rsquare($close,10)": np.array([np.corrcoef(np.arange(10), close[i - 9: i + 1])[0, 1] ** 2 if i >= 9 else np.nan for i in range(len(close))]),
        "Rank($close,10)": np.array([((close[i - 9: i + 1] < close[i]).sum() + 0.5 * (close[i - 9: i + 1] == close[i]).sum()) / 10 if i >= 9 else np.nan for i in range(len(close))]),
        "IdxMax($close,10)": np.array([np.argmax(close[i - 9: i + 1]) + 1 if i >= 9 else np.nan for i in range(len(close))]),
        "Quantile($close,10,0.8)": np.array([np.quantile(close[i - 9: i + 1], 0.8) if i >= 9 else np.nan for i in range(len(close))]),
    }
    for expr, want in checks.items():
        got = ev(expr)[rows, j].double().numpy()
        k = np.arange(len(close)) >= 20  # skip warm-up rows where minimum-count rules differ
        ok = np.isfinite(want) & k
        err = np.nanmax(np.abs(got[ok] - want[ok]) / (np.abs(want[ok]) + 1e-6))
        print(f"{code} {expr:34s} max rel err {err:.2e}" + ("  <-- FAIL" if err > 1e-3 else ""))

lab = d.label(5)
o = d.f["open"].double().numpy()
i, j = 300, d.codes.index("600519")
manual = (o[i + 6, j] / o[i + 1, j] - 1) * 100
raw = (d.f["open"].double()[i + 6] / d.f["open"].double()[i + 1] - 1) * 100
print("label check raw", round(manual, 4), "excess", float(lab[i, j]), "row mean of raw over labelled", float(np.nanmean(np.where(np.isfinite(lab[i].numpy()), raw.numpy(), np.nan))))
t0 = time.perf_counter()
ic = E.daily_ic(-ev("Ref($close,5)/$close") * -1, lab)
print("IC rev_5 (Ref(close,5)/close) PRE", E.ic_stats(ic, d.rows(d.dates[0], E.PRE_END), 5), "SELECT", E.ic_stats(ic, d.rows(E.SELECT_START, E.SELECT_END), 5), f"{time.perf_counter() - t0:.2f}s")
