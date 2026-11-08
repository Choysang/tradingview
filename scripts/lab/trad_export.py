"""Export every traditional rule's daily signals (tradable rows only) to .work/lab/trad_signals.parquet
(columns rule, code, date) for the strategy panel in the tensor lab. Uses trad_battery's rule code as is."""
from __future__ import annotations

import time

import polars as pl

import harness as H
import trad_battery as TB


def main() -> None:
    t0 = time.perf_counter()
    k = H.ctx()
    d, _ = TB.build_base(k.panel, k.benchmark)
    sigs, skipped = TB.run_rules(d, quiet=True)
    ok = d["_ok"].to_numpy()
    base = d.select("code", "date")
    parts = [base.filter(pl.Series(sig & ok)).with_columns(pl.lit(rid).alias("rule")) for rid, sig in sigs.items()]
    out = pl.concat(parts).select("rule", "code", "date")
    out.write_parquet(H.LAB / "trad_signals.parquet")
    print(f"exported {out.height} signals for {len(sigs)} rules (skipped {len(skipped)}) in {time.perf_counter() - t0:.0f}s")


if __name__ == "__main__":
    main()
