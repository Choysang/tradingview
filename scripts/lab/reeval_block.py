"""E082: recompute the bootstrap p values of E058 / E060 / E062 / E070 with a moving-block bootstrap
(docs/research/2026-11-08-block-bootstrap-recompute.md).

For each experiment script: back up the files it writes in .work/lab/main, run it with LAB_BOOT_BLOCK=1 (sameday_base
adds `*_blk` twins of every bootstrap p; the original fields and random stream are unchanged) and LAB_SCORES = the
pinned score file the 11-07 runs read, move the new outputs to .work/lab/main/reeval_block/, restore the originals,
and check that no other file in .work/lab/main changed. Then:
- reproduction: every leaf of the new output that is not a `*_blk` field must equal the original;
- every (p, p_blk) pair is listed; the pre-registered decisions of E062 and E070 are redone with the block p.
Writes .work/lab/main/reeval_block/summary.json.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe reeval_block.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

LAB = Path(__file__).resolve().parents[2] / ".work" / "lab" / "main"
DEST = LAB / "reeval_block"
BACKUP = DEST / "backup"
PINNED = LAB / "pinned" / "fusion_static40_safe_698e3aa7.parquet"
JOBS: tuple[tuple[str, str, tuple[str, ...]], ...] = (  # (experiment, script, files it writes in LAB)
    ("E058", "sequoia_audit.py", ("sequoia_audit.json",)),
    ("E060", "exit_tails.py", ("exit_tails.json", "exit_tails_ma_pullback.parquet", "exit_tails_main_book.parquet",
                               "exit_tails_third_buy.parquet", "exit_tails_third_buy_e050.parquet")),
    ("E060", "trullas_div.py", ("trullas_div.json", "trullas_div_trades.parquet")),
    ("E062", "matched_reeval.py", ("matched_reeval.json",)),
    ("E070", "cond_reversal.py", ("cond_reversal.json",)),
)
P_KEYS = ("p_boot", "p_all", "p_sv", "p_svi", "p_svs")


def stamp() -> dict[str, tuple[int, int]]:
    return {str(f.relative_to(LAB)): (f.stat().st_size, f.stat().st_mtime_ns)
            for f in LAB.rglob("*") if f.is_file() and DEST not in f.parents}


def run_job(script: str, files: tuple[str, ...]) -> dict:
    BACKUP.mkdir(parents=True, exist_ok=True)
    for name in files:
        shutil.copy2(LAB / name, BACKUP / name)
    before = stamp()
    env = {**os.environ, "LAB_BOOT_BLOCK": "1", "LAB_SCORES": str(PINNED), "LAB_UNIVERSE": "main", "PYTHONIOENCODING": "utf-8"}
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, script], cwd=Path(__file__).parent, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    secs = round(time.perf_counter() - t0)
    (DEST / f"{Path(script).stem}.log").write_text(p.stdout + "\n" + p.stderr, encoding="utf-8")
    try:
        if p.returncode == 0:
            for name in files:
                shutil.move(LAB / name, DEST / name)
    finally:
        for name in files:  # the originals are the record; always put them back
            shutil.copy2(BACKUP / name, LAB / name)
    after = stamp()
    changed = sorted(k for k in after if after[k] != before.get(k) and k not in files)
    return {"script": script, "exit": p.returncode, "seconds": secs, "other_files_changed": changed,
            "stderr_tail": p.stderr[-400:] if p.returncode else ""}


def leaves(x, path: str = ""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from leaves(v, f"{path}/{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, x


def reproduction(old, new) -> list[str]:
    o = dict(leaves(old))
    bad = [p for p, v in leaves(new) if not p.rsplit("/", 1)[-1].endswith("_blk") and o.get(p, "<missing>") != v]
    missing = [p for p in o if p not in dict(leaves(new))]
    return bad + [f"missing {m}" for m in missing]


def pairs(x, ctx: dict | None = None, path: str = ""):
    """Every dict with a p field and its *_blk twin: (path, rule, period, hold, n, key, p, p_blk, estimate)."""
    if isinstance(x, dict):
        for key in P_KEYS:
            if key in x and f"{key}_blk" in x:
                est = {"p_boot": x.get("excess"), "p_all": x.get("ex_all"), "p_sv": x.get("ex_sv"), "p_svi": x.get("ex_svi"),
                       "p_svs": x.get("ex_svs")}[key]
                yield {"path": path, "rule": x.get("rule"), "period": x.get("period"), "hold": x.get("hold"), "n": x.get("n"),
                       "key": key, "estimate": est, "p": x[key], "p_blk": x[f"{key}_blk"]}
        for k, v in x.items():
            yield from pairs(v, ctx, f"{path}/{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from pairs(v, ctx, f"{path}[{i}]")


def survives(rows: list[dict], key: str, hold: int = 10) -> bool:
    """sameday_base.survives with the p field named."""
    by = {r["period"]: r for r in rows if r["hold"] == hold}
    pre, dev = by.get("PRE", {}), by.get("DEV", {})
    return bool(pre.get("n") and pre["ex_sv"] > 0 and pre.get(key) is not None and pre[key] < 0.05 and dev.get("n") and dev["ex_sv"] >= 0)


def decisions(new: dict[str, dict]) -> dict:
    out: dict = {}
    m = new.get("matched_reeval.json")
    if m:
        rows = {name: v["rows"] for name, v in m.items()}
        pre10 = {n: next((r for r in rs if r["period"] == "PRE" and r["hold"] == 10), {}) for n, rs in rows.items()}
        raw = lambda key: sorted(n for n, r in pre10.items() if r.get("n") and r["ex_all"] > 0 and r.get(key) is not None and r[key] < 0.05)  # noqa: E731
        out["E062"] = {"raw_sig_old": raw("p_all"), "raw_sig_blk": raw("p_all_blk"),
                       "survives_old": sorted(n for n, rs in rows.items() if survives(rs, "p_sv")),
                       "survives_blk": sorted(n for n, rs in rows.items() if survives(rs, "p_sv_blk"))}
    cr = new.get("cond_reversal.json")
    if cr:
        g = cr["groups"]
        by = lambda name, per, h: next(r for r in g[name] if r["period"] == per and r["hold"] == h)  # noqa: E731
        h4 = lambda key: bool(by("OB_ALL", "PRE", 5)["ex_sv"] < 0 and by("OB_ALL", "PRE", 5)[key] >= 0.95 and by("OB_ALL", "DEV", 5)["ex_sv"] <= 0)  # noqa: E731
        out["E070"] = {"H1_old": survives(g["OS_CLEAN"], "p_sv"), "H1_blk": survives(g["OS_CLEAN"], "p_sv_blk"),
                       "H4_old": h4("p_sv"), "H4_blk": h4("p_sv_blk"),
                       "OS20_GAP_h10": [{k: r.get(k) for k in ("period", "n", "ex_sv", "p_sv", "p_sv_blk")} for r in g["OS20_GAP"] if r["hold"] == 10]}
    return out


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    res: dict = {"jobs": [], "reproduction": {}, "pairs": {}}
    new: dict[str, dict] = {}
    for exp, script, files in JOBS:
        j = run_job(script, files)
        res["jobs"].append({"experiment": exp, **j})
        print(exp, j, flush=True)
        if j["exit"]:
            continue
        for name in files:
            if name.endswith(".json"):
                old = json.loads((LAB / name).read_text(encoding="utf-8"))
                cur = json.loads((DEST / name).read_text(encoding="utf-8"))
                new[name] = cur
                bad = reproduction(old, cur)
                res["reproduction"][name] = {"mismatches": len(bad), "examples": bad[:10]}
                res["pairs"][name] = list(pairs(cur))
                print(name, "mismatches", len(bad), "pairs", len(res["pairs"][name]), flush=True)
    res["decisions"] = decisions(new)
    (DEST / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(res["decisions"], ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
