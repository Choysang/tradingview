"""Down-market evaluation: every playbook and every technical strategy, judged by the months the market fell.

Months are classed by the 中证全指 monthly return: down (< -3%), up (> +3%), flat otherwise. History is
2023-11 .. 2026-07; the current stretch is 2026-08 .. 2026-10 (down months 2026-09, 2026-10).
For each strategy:
- monthly returns from one continuous run over all data (playbooks keep their own rules; technical
  strategies use the shared book with their own holding period);
- fresh-start returns over the last 3 months (from 2026-08-03) and the last month (from 2026-10-08).
Then: does a strategy's record in past down months predict its record in the current down months?
And: do index timing rules (moving average, weekly / monthly MACD) protect a book in down months?
"""
import os
import statistics
import time

import numpy as np
import polars as pl

os.environ["LOCKBOX"] = "1"
import harness as H  # noqa: E402
import tech_signals as T  # noqa: E402
from backend.app.playbooks import exits as lab  # noqa: E402
from backend.app.playbooks.backtest import simulate  # noqa: E402
from backend.app.playbooks.base import catalog  # noqa: E402
from backend.app.playbooks.evaluate import ebb_days, run_signals  # noqa: E402
from contracts.schema import ExitRules, SizingRule  # noqa: E402

c = pl.col
t0 = time.perf_counter()
k = H.ctx()
ebb = ebb_days(k)
CUR = ["2026-08", "2026-09", "2026-10"]
LAST3, LAST1 = "2026-08-03", "2026-10-08"

bm = k.benchmark.select("date", "close").sort("date").with_columns(c("date").dt.strftime("%Y-%m").alias("ym"))
mret = bm.group_by("ym").agg(c("close").last()).sort("ym").with_columns((c("close") / c("close").shift(1) - 1).alias("r")).drop_nulls()
month_class = {ym: ("down" if r < -0.03 else "up" if r > 0.03 else "flat") for ym, r in zip(mret["ym"], mret["r"])}
hist_down = [m for m, k_ in month_class.items() if k_ == "down" and m not in CUR]
hist_up = [m for m, k_ in month_class.items() if k_ == "up" and m not in CUR]
cur_down = [m for m in CUR if month_class.get(m) == "down"]
print("index monthly:", ", ".join(f"{m} {r * 100:+.1f}%" for m, r in zip(mret["ym"], mret["r"])))
print(f"past down months {hist_down}; past up months {len(hist_up)}; current down months {cur_down}", flush=True)


def monthly(eq: pl.DataFrame) -> dict[str, float]:
    m = eq.select("date", "strategy").with_columns(c("date").dt.strftime("%Y-%m").alias("ym")).group_by("ym").agg(
        c("strategy").last()).sort("ym").with_columns((c("strategy") / c("strategy").shift(1) - 1).alias("r")).drop_nulls()
    return dict(zip(m["ym"], m["r"]))


def fresh(sig, exits, sizing, entry, start):
    trades, eq = simulate(k.panel, sig, exits, sizing, k.benchmark, start=start, ebb_days=ebb, entry=entry)
    nav = eq["strategy"].to_numpy()
    return round(float(nav[-1] / 100 - 1) * 100, 2), len(trades)


rows = []


def record(name, label, family, sig, exits, sizing, entry=None):
    _, eq = simulate(k.panel, sig, exits, sizing, k.benchmark, ebb_days=ebb, entry=entry)
    mr = monthly(eq)
    active = [m for m in mr if mr[m] != 0.0]
    if len(active) < 6:
        return
    avg = lambda ms: round(statistics.mean(mr[m] for m in ms if m in mr) * 100, 2) if any(m in mr for m in ms) else None
    l3, n3 = fresh(sig, exits, sizing, entry, LAST3)
    l1, n1 = fresh(sig, exits, sizing, entry, LAST1)
    rows.append({"name": name, "label": label, "family": family, "past_down": avg(hist_down), "past_up": avg(hist_up),
                 "past_all": avg([m for m in mr if m not in CUR]), "cur_down": avg(cur_down),
                 **{m: round(mr.get(m, 0.0) * 100, 2) for m in CUR}, "last3m": l3, "trades3m": n3, "last1m": l1, "trades1m": n1})


for pb in catalog():
    if not lab.testable(pb, k):
        continue
    record(pb.spec.id, pb.spec.name, "playbook", run_signals(pb, k), pb.spec.exits, pb.spec.sizing, pb.spec.entry)
print(f"playbooks done {time.perf_counter() - t0:.0f}s", flush=True)
for name, (sig, hold, label) in T.stock_strategies(k.panel, H.tradable()).items():
    exits = ExitRules(holding_days=hold, stop_loss_pct=None)
    sizing = SizingRule(method="equal", max_positions=20, max_new_per_day=min(20, max(1, round(20 / hold))))
    record(name, label, "technical", H.to_signals(sig, 40), exits, sizing)
print(f"technical done {time.perf_counter() - t0:.0f}s", flush=True)

df = pl.DataFrame(rows)
idx3 = H.index_return(pl.Series([LAST3]).str.to_date()[0], k.dates[-1])
idx1 = H.index_return(pl.Series([LAST1]).str.to_date()[0], k.dates[-1])
pl.Config.set_tbl_rows(80); pl.Config.set_tbl_formatting("ASCII_MARKDOWN"); pl.Config.set_tbl_hide_column_data_types(True)
pl.Config.set_tbl_width_chars(250); pl.Config.set_float_precision(2); pl.Config.set_tbl_cols(20)
print(f"\n中证全指: last 3 months {idx3:+.2f}%, last month {idx1:+.2f}%; monthly avg in past down months "
      f"{statistics.mean(r for m, r in zip(mret['ym'], mret['r']) if m in hist_down) * 100:+.2f}%")
print(df.sort("last3m", descending=True).select("label", "family", "past_down", "past_up", "cur_down", *CUR, "last3m", "trades3m",
                                                 "last1m", "trades1m"))
df.write_csv(H.LAB / "regime_eval.csv")


def carry(pred: str, target: str, top: int = 5) -> str:
    d = df.drop_nulls([pred, target])
    rho = d.select(pl.corr(pred, target, method="spearman")).item()
    best = d.sort(pred, descending=True).head(top)[target].mean()
    return f"{pred:>9s} -> {target:<9s} n {d.height}: rank corr {rho:+.2f}; top-{top} by {pred} average {best:+.2f} vs median {d[target].median():+.2f}"


print("\nDoes the past predict the current down stretch?")
for pred in ("past_down", "past_all", "past_up"):
    print("  " + carry(pred, "cur_down"))
print("  " + carry("2026-09", "2026-10"))
print("  " + carry("past_down", "last1m"))

# Index timing: in the market only while the rule says so (decided at the close, applied from the day after next).
tim = T.index_timing(k.benchmark)
books = {"中证全指": bm.select("date", (c("close") / c("close").shift(1) - 1).fill_null(0.0).alias("r"))}
for pid in ("low_risk_combo", "low_turnover_low_vol"):
    pb = next(p for p in catalog() if p.spec.id == pid)
    _, eq = simulate(k.panel, run_signals(pb, k), pb.spec.exits, pb.spec.sizing, k.benchmark, ebb_days=ebb, entry=pb.spec.entry)
    books[pb.spec.name] = eq.select("date", (c("strategy") / c("strategy").shift(1) - 1).fill_null(0.0).alias("r"))
print("\nIndex timing overlays (0.1% per switch); monthly averages in past down / up months, current months, total")
for bname, b in books.items():
    j = b.join(tim, on="date", how="left").fill_null(1.0).sort("date")
    for rule in ["always", *[col for col in tim.columns if col != "date"]]:
        w = np.ones(j.height) if rule == "always" else j[rule].to_numpy()
        lag = np.concatenate([[1.0, 1.0], w[:-2]])
        r = j["r"].to_numpy() * lag - 0.001 * np.abs(np.diff(np.concatenate([[1.0], lag])))
        m = pl.DataFrame({"date": j["date"], "r": r}).with_columns(c("date").dt.strftime("%Y-%m").alias("ym")).group_by("ym").agg(
            ((1 + c("r")).product() - 1).alias("m")).sort("ym")
        mm = dict(zip(m["ym"], m["m"]))
        f = lambda ms: statistics.mean(mm[x] for x in ms if x in mm) * 100
        tot = (np.prod(1 + r) - 1) * 100
        cur3 = (np.prod([1 + mm.get(x, 0.0) for x in CUR]) - 1) * 100
        print(f"  {bname:8s} {rule:20s} past down {f(hist_down):+6.2f}% up {f(hist_up):+6.2f}% | 08 {mm['2026-08'] * 100:+6.2f}% "
              f"09 {mm['2026-09'] * 100:+6.2f}% 10 {mm['2026-10'] * 100:+6.2f}% last3m {cur3:+6.2f}% | all {tot:+7.2f}% "
              f"in-market {lag.mean() * 100:4.0f}%")
print(f"done {time.perf_counter() - t0:.0f}s")
