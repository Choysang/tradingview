"""Convert lab score grids (.pt, [days x stocks]) to long parquet (code, date, score) for the backend
engine (official_eval.py). Usage: python export_scores.py fusion_static40.pt [more.pt ...]"""
import sys

import numpy as np
import polars as pl
import torch

import tengine as E

d = E.Data()
for name in sys.argv[1:]:
    s = torch.load(E.LAB / name)
    m = torch.isfinite(s) & d.tradable
    ti, ni = torch.nonzero(m, as_tuple=True)
    out = pl.DataFrame({"code": [d.codes[i] for i in ni.tolist()], "date": pl.Series([d.dates[i] for i in ti.tolist()], dtype=pl.Date),
                        "score": s[ti, ni].double().numpy()})
    out.write_parquet(E.LAB / name.replace(".pt", ".parquet"))
    print(name, out.height, "rows")
