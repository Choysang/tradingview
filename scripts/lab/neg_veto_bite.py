"""Hit-rate check for a negative-announcement veto on the frozen main book (written down before running in
docs/research/2026-11-08-overbought-veto-preregistration.md, section 8).

Counts only: how many of the frozen book's buys (V0 of overbought_veto.py, ungated and gated) had a main-board
cninfo announcement of a negative class (batch3_lib.neg_class, as E070) dated in [the session 5 back, D], where
D = the session before the buy; the 20-session window is a reference. Returns of these buys are not read, so a
later replay stays a clean test. Worth a pre-registered replay (E080) when the 5-session hit share of the ungated
buys is >= 2% and the DEV segment has >= 10 hits.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe neg_veto_bite.py
"""
from __future__ import annotations

import json

import polars as pl

import batch3_lib as L
import curve_gate as G
import forward_book as F
import harness as H
import overbought_veto as V
from harness import CODE, c

OUT = H.LAB / "neg_veto_bite.json"
WINDOWS = (5, 20)
MIN_SHARE, MIN_DEV = 0.02, 10


def negatives() -> pl.DataFrame:
    ann = pl.read_parquet(H.BASE / "ext" / "cninfo_ann.parquet").filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    ann = ann.with_columns(c("title").map_elements(L.neg_class, return_dtype=pl.Utf8).alias("cls")).filter(c("cls").is_not_null())
    return ann.unique(["id"]).select(CODE, "ann_date", "cls")


def hits(b: pl.DataFrame, neg: pl.DataFrame, n: int) -> pl.DataFrame:
    """Per buy (code, date = D): hit flag and the classes of the negative announcements in [D - n sessions, D]."""
    days = list(H.ctx().dates)
    pos = {d: i for i, d in enumerate(days)}
    b = b.with_columns(pl.Series("start", [days[max(0, pos[d] - n)] for d in b["date"].to_list()]))
    j = b.join(neg, on=CODE, how="inner").filter((c("ann_date") >= c("start")) & (c("ann_date") <= c("date")))
    cls = j.group_by(CODE, "entry").agg(c("cls").unique().sort().alias("classes"))
    return b.join(cls, on=[CODE, "entry"], how="left").with_columns(c("classes").is_not_null().alias("hit"))


def summary(h: pl.DataFrame) -> dict:
    out = {}
    for name, a, e in V.SEGMENTS:
        s = h.filter(V.in_seg(a, e))
        k = s.filter(c("hit"))
        by = k.explode("classes").group_by("classes").len().sort("len", descending=True).rows()
        out[name] = {"buys": s.height, "hits": k.height, "share": round(k.height / s.height, 4) if s.height else None, "by_class": by}
    return out


def main() -> None:
    scores = pl.read_parquet(V.PINNED).select(CODE, "date", "score")
    t0, e0 = V.run_book(scores, 5)
    tg, _ = V.run_book(scores, 5, G.gate(e0, look=F.LOOK, low=F.LOW))
    neg = negatives()
    res: dict = {"negatives_main": neg.height, "rule": {"min_share": MIN_SHARE, "min_dev": MIN_DEV}}
    for book, t in (("ungated", t0), ("gated", tg)):
        b = V.buys(t)
        res[book] = {f"w{n}": summary(hits(b, neg, n)) for n in WINDOWS}
    m = res["ungated"]["w5"]
    total = m["PRE"]["buys"] + m["DEV"]["buys"]
    share = (m["PRE"]["hits"] + m["DEV"]["hits"]) / total if total else 0.0
    res["share_all_w5"] = round(share, 4)
    res["worth_replay"] = bool(share >= MIN_SHARE and m["DEV"]["hits"] >= MIN_DEV)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
