"""Factor zoo in tengine syntax: Qlib Alpha158 (copied from qlib/contrib/data/loader.py, MIT) plus
A-share extras (turnover, limit-ups, size, beta, liquidity). Run in the lab venv:

    python zoo.py            # IC table for every zoo factor on PRE and SELECT -> .work/lab/zoo_ic.csv
"""
from __future__ import annotations

import sys
import time

import numpy as np
import polars as pl

import tengine as E

WINDOWS = (5, 10, 20, 30, 60)


def alpha158() -> dict[str, str]:
    z = {
        "KMID": "($close-$open)/$open", "KLEN": "($high-$low)/$open",
        "KMID2": "($close-$open)/($high-$low+1e-12)", "KUP": "($high-Greater($open, $close))/$open",
        "KUP2": "($high-Greater($open, $close))/($high-$low+1e-12)", "KLOW": "(Less($open, $close)-$low)/$open",
        "KLOW2": "(Less($open, $close)-$low)/($high-$low+1e-12)", "KSFT": "(2*$close-$high-$low)/$open",
        "KSFT2": "(2*$close-$high-$low)/($high-$low+1e-12)",
        "OPEN0": "$open/$close", "HIGH0": "$high/$close", "LOW0": "$low/$close", "VWAP0": "$vwap/$close",
    }
    for d in WINDOWS:
        z |= {
            f"ROC{d}": f"Ref($close, {d})/$close", f"MA{d}": f"Mean($close, {d})/$close", f"STD{d}": f"Std($close, {d})/$close",
            f"BETA{d}": f"Slope($close, {d})/$close", f"RSQR{d}": f"Rsquare($close, {d})", f"RESI{d}": f"Resi($close, {d})/$close",
            f"MAX{d}": f"Max($high, {d})/$close", f"MIN{d}": f"Min($low, {d})/$close",
            f"QTLU{d}": f"Quantile($close, {d}, 0.8)/$close", f"QTLD{d}": f"Quantile($close, {d}, 0.2)/$close",
            f"RANK{d}": f"Rank($close, {d})", f"RSV{d}": f"($close-Min($low, {d}))/(Max($high, {d})-Min($low, {d})+1e-12)",
            f"IMAX{d}": f"IdxMax($high, {d})/{d}", f"IMIN{d}": f"IdxMin($low, {d})/{d}",
            f"IMXD{d}": f"(IdxMax($high, {d})-IdxMin($low, {d}))/{d}",
            f"CORR{d}": f"Corr($close, Log($volume+1), {d})",
            f"CORD{d}": f"Corr($close/Ref($close,1), Log($volume/Ref($volume, 1)+1), {d})",
            f"CNTP{d}": f"Mean($close>Ref($close, 1), {d})", f"CNTN{d}": f"Mean($close<Ref($close, 1), {d})",
            f"CNTD{d}": f"Mean($close>Ref($close, 1), {d})-Mean($close<Ref($close, 1), {d})",
            f"SUMP{d}": f"Sum(Greater($close-Ref($close, 1), 0), {d})/(Sum(Abs($close-Ref($close, 1)), {d})+1e-12)",
            f"SUMN{d}": f"Sum(Greater(Ref($close, 1)-$close, 0), {d})/(Sum(Abs($close-Ref($close, 1)), {d})+1e-12)",
            f"SUMD{d}": f"(Sum(Greater($close-Ref($close, 1), 0), {d})-Sum(Greater(Ref($close, 1)-$close, 0), {d}))/(Sum(Abs($close-Ref($close, 1)), {d})+1e-12)",
            f"VMA{d}": f"Mean($volume, {d})/($volume+1e-12)", f"VSTD{d}": f"Std($volume, {d})/($volume+1e-12)",
            f"WVMA{d}": f"Std(Abs($close/Ref($close, 1)-1)*$volume, {d})/(Mean(Abs($close/Ref($close, 1)-1)*$volume, {d})+1e-12)",
            f"VSUMP{d}": f"Sum(Greater($volume-Ref($volume, 1), 0), {d})/(Sum(Abs($volume-Ref($volume, 1)), {d})+1e-12)",
            f"VSUMN{d}": f"Sum(Greater(Ref($volume, 1)-$volume, 0), {d})/(Sum(Abs($volume-Ref($volume, 1)), {d})+1e-12)",
            f"VSUMD{d}": f"(Sum(Greater($volume-Ref($volume, 1), 0), {d})-Sum(Greater(Ref($volume, 1)-$volume, 0), {d}))/(Sum(Abs($volume-Ref($volume, 1)), {d})+1e-12)",
        }
    return z


def extras() -> dict[str, str]:
    z = {
        "SIZE": "Log($cap)", "TURN1": "$turn", "AMT20": "Log(Mean($amount, 20))",
        "BETA_MKT60": "Cov($ret, $mkt, 60)/(Var($mkt, 60)+1e-12)",
        "IVOL20": "Std($ret-$mkt, 20)", "AMIHUD20": "Mean(Abs($ret)/($amount+1e-6), 20)",
        "GAP20": "Mean($open/Ref($close, 1)-1, 20)", "INTRA20": "Mean($close/$open-1, 20)",
        "GAP1": "$open/Ref($close, 1)-1", "MAXRET20": "Max($ret, 20)", "MINRET20": "Min($ret, 20)",
        "ZT5": "Sum($is_limit_up, 5)", "ZT20": "Sum($is_limit_up, 20)", "ZT60": "Sum($is_limit_up, 60)",
        "DT20": "Sum($is_limit_down, 20)", "BROKEN20": "Sum($is_broken, 20)", "STREAK": "$limit_streak",
        "ROC120": "Ref($close, 120)/$close", "ROC250": "Ref($close, 250)/$close",
        "MOM_120_20": "Ref($close, 20)/Ref($close, 120)", "MA250": "Mean($close, 250)/$close",
        "MAX250": "Max($high, 250)/$close", "MIN250": "Min($low, 250)/$close",
        "RET_MKT_CORR20": "Corr($ret, $mkt, 20)", "UPDOWN_VOL20": "Mean(Greater($ret, 0)*$volume, 20)/(Mean(Less($ret, 0)*(0-1)*$volume, 20)+1e-12)",
    }
    for d in (5, 20, 60):
        z |= {f"TURN_MA{d}": f"Mean($turn, {d})", f"TURN_STD{d}": f"Std($turn, {d})", f"TURN_REL{d}": f"$turn/(Mean($turn, {d})+1e-12)",
              f"AMP{d}": f"Mean(($high-$low)/Ref($close, 1), {d})", f"RET_STD{d}": f"Std($ret, {d})"}
    return z


def behavioral() -> dict[str, str]:
    """Behavioural-finance factors (human biases), each with its source:
    CGO: disposition effect, Grinblatt & Han (2005); TK: prospect theory, Barberis et al. (2016);
    SALIENCE: salience theory, Bordalo et al. (2013) / Cosemans & Frehen (2021); MAXQ: lottery demand,
    Bali et al. (2011); TOW: overnight-vs-intraday tug of war, Lou, Polk & Skouras (2019); SKEW: skewness
    preference; ATTN: attention / abnormal turnover, Barber & Odean (2008); PANIC and FOMO: limit-down
    panic and limit-up chasing (A-share retail behaviour)."""
    dev = "($ret-Mean($ret, 20))"
    return {
        "BEH_CGO": "$cgo", "BEH_TK60": "$tk",
        "BEH_SALIENCE20": "Cov(Abs($ret-$mkt)/(Abs($ret)+Abs($mkt)+0.1), $ret, 20)",
        "BEH_MAXQ20": "Quantile($ret, 20, 0.9)",
        "BEH_TOW20": "Mean($open/Ref($close, 1)-1, 20)-Mean($close/$open-1, 20)",
        "BEH_SKEW20": f"Mean({dev}*{dev}*{dev}, 20)/(Std($ret, 20)*Std($ret, 20)*Std($ret, 20)+1e-9)",
        "BEH_ATTN5": "Mean($turn, 5)/(Mean($turn, 60)+1e-9)",
        "BEH_PANIC5": "Sum($is_limit_down, 5)", "BEH_FOMO5": "Sum($is_limit_up, 5)*Mean($turn, 5)",
        "BEH_CGO_VOL": "$cgo*Std($ret, 60)",
    }


def zoo() -> dict[str, str]:
    return alpha158() | extras() | behavioral()


def ic_table(names: dict[str, str], data: E.Data, horizons=(5, 10, 20)) -> pl.DataFrame:
    ev = E.Evaluator(data, cache_size=64)
    pre = data.rows(data.dates[0], E.PRE_END)
    sel = data.rows(E.SELECT_START, E.SELECT_END)
    labels = {h: data.label(h) for h in horizons}
    rows = []
    for i, (name, expr) in enumerate(names.items()):
        x = ev(expr)
        row = {"name": name, "expr": expr, "coverage": round(E.coverage(x, data), 3)}
        for h, y in labels.items():
            ic = E.daily_ic(x, y)
            for tag, r in (("pre", pre), ("sel", sel)):
                s = E.ic_stats(ic, r, h)
                row |= {f"ic{h}_{tag}": s["ic"], f"t{h}_{tag}": s["t"]}
        rows.append(row)
        if i % 25 == 0:
            print(f"  [{i}/{len(names)}] {name}", flush=True)
    return pl.DataFrame(rows)


if __name__ == "__main__":
    t0 = time.perf_counter()
    data = E.Data()
    z = zoo()
    tab = ic_table(z, data)
    tab.write_csv(E.LAB / "zoo_ic.csv")
    print(f"zoo {len(z)} factors in {time.perf_counter() - t0:.0f}s")
    with pl.Config(tbl_rows=40, tbl_cols=12, fmt_float="mixed"):
        cols = ["name", "coverage", "ic5_pre", "t5_pre", "ic5_sel", "t5_sel", "ic20_pre", "t20_pre", "ic20_sel", "t20_sel"]
        print("top by |t5_sel|:")
        print(tab.with_columns(pl.col("t5_sel").abs().alias("a")).sort("a", descending=True).head(25).select(cols))
        print("top by |t5_pre|:")
        print(tab.with_columns(pl.col("t5_pre").abs().alias("a")).sort("a", descending=True).head(15).select(cols))
        same = tab.filter((pl.col("ic5_pre") * pl.col("ic5_sel") > 0) & (pl.col("t5_pre").abs() > 3) & (pl.col("t5_sel").abs() > 2))
        flip = tab.filter((pl.col("ic5_pre") * pl.col("ic5_sel") < 0) & (pl.col("t5_pre").abs() > 3) & (pl.col("t5_sel").abs() > 2))
        print(f"h5: same sign and |t|>3 PRE, |t|>2 SELECT: {same.height}; flipped: {flip.height}")
        print("flipped:", flip.select("name", "ic5_pre", "ic5_sel").head(20).rows())
    sys.exit(0)
