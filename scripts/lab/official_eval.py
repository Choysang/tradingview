"""Official numbers for lab scores on the backend engine (T+1, limit-up/limit-down fills, costs), via
harness.evaluate: each day the best 40 names are offered, 20 slots, hold 5 days, no stops.
Usage: python official_eval.py fusion_static40.parquet [...]; periods SELECT, plus VERIFY with env VERIFY=1."""
import os
import sys

import polars as pl

import harness as H

periods = ("PRE", "SELECT") + (("VERIFY",) if os.environ.get("VERIFY") == "1" else ())
for name in sys.argv[1:]:
    s = pl.read_parquet(H.LAB / name)
    res = H.evaluate(s, f"r5:{name}", hold=5, positions=20, offer=40, periods=periods, note="round 5 official")
    print(H.show(res), flush=True)
