"""Daily forward run on this PC (backend venv): server warehouse snapshot -> panel -> fusion scores -> both books.

The server is the only writer of the live warehouse; this script only reads it. Steps:
1. Mirror C:/pulse/app/backend/data/warehouse/live from the server into .work/snapshots/server/data/warehouse/live
   (only files whose size or time changed; local files gone from the server are removed). A file the server
   touched in the last two minutes means a sync is running there: wait, retry, then give up.
2. Stop when the snapshot has no session after the last one logged by the frozen book, or when the latest session
   fails the coverage gate (backend/app/domain/coverage_gate.py: < 95% of the main-board codes that had a bar the
   session before); coverage of every new session is logged.
3. export_panel.py (lab panel, every board) and the 上证 benchmark (index_bars 000001) with PULSE_DATA_DIR on the
   snapshot.
4. fusion.py in the lab venv with the recipe that made fusion_static40_safe (LAB_UNIVERSE=main, FUSION_SAFE=1,
   FUSION_POOL_TOP=200, static models, VERIFY=1 so rows after 2026-08 are scored) under the tag _safe_new, then
   export_scores.py.
5. Reproduction check: on the last 60 sessions both files score, the new top 40 must overlap the old top 40 by
   >= 95% on average (expanding-window weights are point in time, so old days should not move). Fail -> stop,
   keep the old scores, log the reason.
6. Promote: archive the old score files under .work/lab/main/archive/<stamp>/, copy the new ones over.
7. forward_book.py (frozen, decision of record), forward_book_h10.py (E065 shadow), forward_monitor.py (E068:
   IC, decile spread and matched excess of the frozen score on every decision date whose label is complete) and
   shadow_x20.py (E085: the R10 X20 basket against the equal-weight pool).
8. os20_forward.py (E081: 20-day shock-type oversold rebound, frozen signal lists and their 10-day outcomes). It
   runs last and its failure is logged without failing the run, so the books' records are never affected.
9. The frozen book's holdings and next buys go to the server as C:/pulse/data/holdings/book.json, read by the news
   alert process (E075, news_alerts.py): holdings, watch = the names it will try to buy plus the offer's top 10.
   A failure is logged and does not fail the run.
10. E092: llm_live.py prepares the day's web-search LLM jobs and sends them to the server, which holds the key. Without
   the switch file .work/lab/live/llm_live.enabled this is a free dry run (llm_ask.py check); with it the server makes
   the paid calls (llm_ask.py run) and the verdicts come back. A failure is logged and does not fail the run.
When step 2 finds no new session but step 1 copied files (a server data revision, such as a backfilled session),
the panel is refreshed and only forward_monitor.py and os20_forward.py run, so flagged dates whose window now passes the gate are
evaluated again; the books are not rerun. `--revision` forces this path.
Each run appends to .work/lab/live/daily_forward.log. Usage: python daily_forward.py [--skip-sync] [--revision]
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABDIR = ROOT / "scripts" / "lab"
WORK = ROOT / ".work"
SNAP = WORK / "snapshots" / "server" / "data"
LOCAL_LIVE = SNAP / "warehouse" / "live"
MANIFEST = SNAP / "manifest.json"
MAIN = WORK / "lab" / "main"
LOG = WORK / "lab" / "live" / "daily_forward.log"
LOCK = WORK / "lab" / "live" / "daily_forward.lock"
BACKEND_PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
LAB_PY = WORK / "labenv" / ".venv" / "Scripts" / "python.exe"
# user@host of the Pulse server: env PULSE_SERVER, else the first line of .work/server_host (git-ignored; the public
# repository never carries the address)
HOST_FILE = WORK / "server_host"


def server_host() -> str:
    if os.environ.get("PULSE_SERVER"):
        return os.environ["PULSE_SERVER"]
    lines = HOST_FILE.read_text(encoding="utf-8").split() if HOST_FILE.exists() else []
    return lines[0] if lines else ""


HOST = server_host()
REMOTE_LIVE = r"C:\pulse\app\backend\data\warehouse\live"
SSH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH"
KEY = Path.home() / ".ssh" / "pulse_server"
KNOWN = Path.home() / ".ssh" / "known_hosts"
SSH_OPTS = ["-i", str(KEY), "-o", "BatchMode=yes", "-o", f"UserKnownHostsFile={KNOWN}", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=20"]
FRESH_S = 120
OVERLAP_MIN, CHECK_DAYS = 0.95, 60
SCORE = "fusion_static40_safe"
NEW = "fusion_static40_safe_new"


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run(cmd: list[str], env: dict | None = None, cwd: Path = LABDIR, timeout: int = 7200) -> str:
    e = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT), **(env or {})}
    p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} {' '.join(cmd[1:3])} exit {p.returncode}: {(p.stderr or p.stdout)[-800:]}")
    return p.stdout


def remote_files() -> dict[str, tuple[int, str]]:
    ps = (f"$r='{REMOTE_LIVE}'; Get-ChildItem -Recurse -File $r | ForEach-Object {{ $_.FullName.Substring($r.Length+1) + '|' + "
          f"$_.Length + '|' + $_.LastWriteTimeUtc.ToString('o') }}")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    out = run([str(SSH / "ssh.exe"), *SSH_OPTS, HOST, f"powershell -NoProfile -EncodedCommand {enc}"], cwd=ROOT, timeout=120)
    files = {}
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3:
            files[parts[0].replace("\\", "/")] = (int(parts[1]), parts[2])
    if not files:
        raise RuntimeError("empty remote listing")
    return files


def sync() -> int:
    for attempt in range(3):
        files = remote_files()
        now = datetime.now(timezone.utc)
        fresh = [k for k, (_, t) in files.items() if (now - datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds() < FRESH_S]
        if not fresh:
            break
        log(f"sync: {len(fresh)} files changed in the last {FRESH_S}s on the server, waiting (attempt {attempt + 1})")
        time.sleep(90)
    else:
        raise RuntimeError("server warehouse kept changing; snapshot skipped")
    old = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    todo = [k for k, v in files.items() if old.get(k) != list(v) or not (LOCAL_LIVE / k).exists()]
    for k in todo:
        dst = LOCAL_LIVE / k
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        src = f"{HOST}:{REMOTE_LIVE}\\{k}".replace("\\", "/")
        run([str(SSH / "scp.exe"), "-q", *SSH_OPTS, src, str(tmp)], cwd=ROOT, timeout=600)
        if tmp.stat().st_size != files[k][0]:
            raise RuntimeError(f"size mismatch for {k}")
        os.replace(tmp, dst)
    gone = [k for k in old if k not in files]
    for k in gone:
        (LOCAL_LIVE / k).unlink(missing_ok=True)
    MANIFEST.write_text(json.dumps({k: list(v) for k, v in files.items()}, ensure_ascii=False), encoding="utf-8")
    log(f"sync: {len(files)} files, {len(todo)} copied, {len(gone)} removed")
    return len(todo)


def snapshot_last_session() -> str:
    code = ("from backend.app.warehouse.store import warehouse; import polars as pl; "
            "print(warehouse('live').scan('bars').select(pl.col('date').max()).collect().item())")
    return run([str(BACKEND_PY), "-c", code], env={"PULSE_DATA_DIR": str(SNAP)}, cwd=ROOT).strip().splitlines()[-1]


def coverage(since: str | None) -> list[dict]:
    """backend.app.domain.coverage_gate per session after `since`: main-board codes with a bar / main-board codes with
    a bar on the previous session (a proxy for 'expected to trade')."""
    code = f"""
import json, polars as pl
from backend.app.warehouse.store import warehouse
from backend.app.domain.coverage_gate import decide
MAIN = ('600', '601', '603', '605', '000', '001', '002', '003')
b = warehouse('live').scan('bars').filter(pl.col('code').str.slice(0, 3).is_in(MAIN)).select('code', 'date').collect()
days = sorted(b['date'].unique().to_list())
by = {{d: set(g['code'].to_list()) for (d,), g in b.partition_by('date', as_dict=True).items()}}
out = []
for prev, d in zip(days, days[1:]):
    if {since!r} is None or str(d) > {since!r}:
        r = decide(by[prev], by[d])
        out.append({{'date': str(d), 'coverage': r.coverage, 'status': r.status, 'missing': len(r.missing)}})
print(json.dumps(out[-10:]))
"""
    return json.loads(run([str(BACKEND_PY), "-c", code], env={"PULSE_DATA_DIR": str(SNAP)}, cwd=ROOT).strip().splitlines()[-1])


def last_logged() -> str | None:
    p = WORK / "lab" / "live" / "gated_book.jsonl"
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []
    return max((r["date"] for r in rows), default=None)


def check_reproduction() -> dict:
    code = f"""
import json, polars as pl
old = pl.read_parquet(r'{MAIN / (SCORE + ".parquet")}'); new = pl.read_parquet(r'{MAIN / (NEW + ".parquet")}')
days = sorted(set(old['date'].unique().to_list()) & set(new['date'].unique().to_list()))[-{CHECK_DAYS}:]
top = lambda f, d: set(f.filter(pl.col('date') == d).sort('score', descending=True)['code'].head(40).to_list())
ov = [len(top(old, d) & top(new, d)) / 40 for d in days]
print(json.dumps({{'days': len(days), 'mean_overlap': sum(ov) / max(len(ov), 1), 'min_overlap': min(ov) if ov else None,
                  'old_last': str(old['date'].max()), 'new_last': str(new['date'].max())}}))
"""
    return json.loads(run([str(BACKEND_PY), "-c", code], cwd=ROOT).strip().splitlines()[-1])


ASOF = WORK / "lab" / "live" / "scores_asof.parquet"


def archive_asof() -> str:
    """Append the score rows of every date not yet archived (append-only): the scores as first computed, so later
    recomputations on revised data never rewrite what the forward record saw."""
    code = f"""
import polars as pl
from pathlib import Path
new = pl.read_parquet(r'{MAIN / (SCORE + ".parquet")}').with_columns(pl.lit(__import__('datetime').date.today()).alias('computed_on'))
p = Path(r'{ASOF}')
old = pl.read_parquet(p) if p.exists() else None
have = set(old['date'].unique().to_list()) if old is not None else set()
start = max(have) if have else __import__('datetime').date(2026, 10, 30)
add = new.filter((pl.col('date') > start) & ~pl.col('date').is_in(list(have)))
out = add if old is None else pl.concat([old, add])
tmp = p.with_suffix('.tmp'); out.write_parquet(tmp); tmp.replace(p)
print(add['date'].n_unique(), 'dates added;', out['date'].n_unique(), 'dates archived')
"""
    return run([str(BACKEND_PY), "-c", code], cwd=ROOT).strip().splitlines()[-1]


def promote() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    arch = MAIN / "archive" / stamp
    arch.mkdir(parents=True, exist_ok=True)
    for ext in (".pt", ".parquet"):
        old, new = MAIN / (SCORE + ext), MAIN / (NEW + ext)
        if old.exists():
            shutil.copy2(old, arch / old.name)
        shutil.copy2(new, old)
    return arch


BOOK_ENV = {"PULSE_DATA_DIR": str(SNAP), "LAB_UNIVERSE": "main"}


def run_os20() -> None:
    """Step 8 (E081): a failure is logged and does not fail the run."""
    try:
        log("os20_forward.py: " + run([str(BACKEND_PY), "os20_forward.py"], env=BOOK_ENV).strip().splitlines()[-1][:600])
    except Exception as e:  # noqa: BLE001 - the books ran already; this log must not mark the day as failed
        log(f"os20_forward.py FAILED (books unaffected): {type(e).__name__}: {str(e)[:400]}")


REMOTE_HOLDINGS = "C:/pulse/data/holdings/book.json"
HOLDINGS = WORK / "lab" / "live" / "holdings_book.json"


def holdings_book(lines: list[str]) -> dict | None:
    """The news alert input from the frozen book's last logged decision."""
    rows = [json.loads(x) for x in lines if x.strip()]
    if not rows:
        return None
    last = rows[-1]
    held = sorted(set(last.get("holdings") or []))
    watch = sorted(set((last.get("try_buy") or []) + (last.get("offer_top10") or [])) - set(held))
    return {"date": last["date"], "holdings": held, "watch": watch, "source": "gated_book"}


def push_holdings() -> None:
    """Step 9 (E075): a failure is logged and does not fail the run."""
    try:
        book = holdings_book((MAIN.parent / "live" / "gated_book.jsonl").read_text(encoding="utf-8").splitlines())
        if book is None:
            log("holdings: no frozen-book row yet; nothing pushed")
            return
        HOLDINGS.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        ps = r"New-Item -ItemType Directory -Force C:\pulse\data\holdings | Out-Null"
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        run([str(SSH / "ssh.exe"), *SSH_OPTS, HOST, f"powershell -NoProfile -EncodedCommand {enc}"], cwd=ROOT, timeout=120)
        run([str(SSH / "scp.exe"), "-q", *SSH_OPTS, str(HOLDINGS), f"{HOST}:{REMOTE_HOLDINGS}"], cwd=ROOT, timeout=120)
        log(f"holdings: pushed {book['date']} ({len(book['holdings'])} held, {len(book['watch'])} watched)")
    except Exception as e:  # noqa: BLE001 - the books ran already
        log(f"holdings push FAILED (books unaffected): {type(e).__name__}: {str(e)[:300]}")


LLM_SWITCH = WORK / "lab" / "live" / "llm_live.enabled"
LLM_DECISIONS = MAIN / "live" / "decisions.jsonl"
REMOTE_LLM = "C:/pulse/data/llm"  # outside the application folder: the call ledger holds the spending limits
REMOTE_PY = r"C:\pulse\app\backend\.venv\Scripts\python.exe"


def remote_ps(ps: str, timeout: int = 120) -> str:
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    return run([str(SSH / "ssh.exe"), *SSH_OPTS, HOST, f"powershell -NoProfile -EncodedCommand {enc}"], cwd=ROOT, timeout=timeout)


def pull_llm_decisions() -> None:
    """Mirror the server's verdicts. Rows held here that the server has lost are kept and reported."""
    from llm_ask import read_rows
    LLM_DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    tmp = LLM_DECISIONS.with_suffix(".part")
    run([str(SSH / "scp.exe"), "-q", *SSH_OPTS, f"{HOST}:{REMOTE_LLM}/decisions.jsonl", str(tmp)], cwd=ROOT, timeout=120)
    remote = read_rows(tmp)
    ids = {r.get("id") for r in remote}
    lost = [r for r in read_rows(LLM_DECISIONS) if r.get("id") not in ids]
    if lost:
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lost + remote), encoding="utf-8")
        log(f"llm_live FAILED: the server's decisions lack {len(lost)} ids held here; both kept in the local file")
    os.replace(tmp, LLM_DECISIONS)


def llm_flag(mode: str, result: dict) -> str:
    """A paid run that did not finish, or a key file that cannot work, has to stand out in the morning check."""
    bad = result.get("status") not in ("done", "limit") if mode == "run" else result.get("refusal") == "bad_key"
    return " FAILED" if bad else ""


def run_llm_live() -> None:
    """Step 10 (E092): a failure is logged and does not fail the run."""
    try:
        made = run([str(LAB_PY), "llm_live.py", "prepare"], env={"LAB_UNIVERSE": "main", "LIVE_SCORE": SCORE + ".pt"})
        jobs = Path(made.strip().splitlines()[-1])
        mode = "run" if LLM_SWITCH.exists() else "check"
        remote = f"{REMOTE_LLM}/{jobs.name}"
        remote_ps(f"New-Item -ItemType Directory -Force '{REMOTE_LLM}' | Out-Null")
        run([str(SSH / "scp.exe"), "-q", *SSH_OPTS, str(jobs), f"{HOST}:{remote}"], cwd=ROOT, timeout=120)
        ps = (rf"Set-Location C:\pulse\app; $env:PYTHONUTF8='1'; $env:PULSE_LLM_DIR='{REMOTE_LLM}'; "
              rf"& '{REMOTE_PY}' scripts\lab\llm_ask.py {mode} '{remote}'; exit $LASTEXITCODE")
        line = remote_ps(ps, timeout=7200).strip().splitlines()[-1]
        result = json.loads(line)
        log(f"llm_live {mode}{llm_flag(mode, result)}: {line[:600]}")
        if result.get("logged"):  # also in a dry run, so verdicts a failed pull left behind still come home
            pull_llm_decisions()
    except Exception as e:  # noqa: BLE001 - the books ran already
        log(f"llm_live FAILED (books unaffected): {type(e).__name__}: {str(e)[-400:]}")


def refresh_panel() -> None:
    """export_panel.py (every board) and the 上证 benchmark from index_bars 000001, both on the snapshot."""
    snap_env = {"PULSE_DATA_DIR": str(SNAP)}
    log("export_panel: " + run([str(BACKEND_PY), "export_panel.py"], env={**snap_env, "LAB_UNIVERSE": "all"}).strip().splitlines()[-1])
    bench = ("from backend.app.warehouse.store import warehouse; import polars as pl; "
             "b = warehouse('live').scan('index_bars').filter(pl.col('symbol') == '000001').select('date', 'open', 'close')"
             ".collect().unique('date', keep='last').sort('date'); "
             f"b.write_parquet(r'{WORK / 'lab' / 'benchmark_sh.parquet'}'); print(b.height, b['date'].max())")
    log("benchmark_sh: " + run([str(BACKEND_PY), "-c", bench], env=snap_env, cwd=ROOT).strip())


def main() -> None:
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 6 * 3600:
        log("another run holds the lock; exit")
        return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    t0 = time.perf_counter()
    try:
        copied = sync() if "--skip-sync" not in sys.argv else 0
        last_bar, logged = snapshot_last_session(), last_logged()
        log(f"snapshot last session {last_bar}; frozen book last logged {logged}")
        if logged and last_bar <= logged:
            if copied or "--revision" in sys.argv:
                # A server data revision (e.g. a backfilled session): flagged monitor dates may now pass the coverage
                # gate. The books are not rerun; their logs stay the decision record.
                log("no new session but the snapshot changed: refresh the panel and re-evaluate the monitor")
                refresh_panel()
                log("forward_monitor.py: " + run([str(BACKEND_PY), "forward_monitor.py"], env=BOOK_ENV).strip().splitlines()[-1][:600])
                run_os20()
            else:
                log("no new session; nothing to do")
            return
        cov = coverage(logged)
        log(f"coverage: {cov}")
        if cov and cov[-1]["status"] == "refuse":
            log(f"STOP: latest session {cov[-1]['date']} coverage {cov[-1]['coverage']:.3f} < 0.95 (coverage gate); books not run")
            return
        refresh_panel()
        fenv = {"LAB_UNIVERSE": "main", "FUSION_SAFE": "1", "FUSION_TAG": "_safe_new", "FUSION_POOL_TOP": "200",
                "FUSION_MODELS": "static", "VERIFY": "1"}
        out = run([str(LAB_PY), "-u", "fusion.py"], env=fenv, timeout=4 * 3600)
        log("fusion: " + " / ".join(x for x in out.splitlines() if "static k=40" in x or "level check" in x)[:400])
        log("export_scores: " + run([str(LAB_PY), "export_scores.py", NEW + ".pt"], env={"LAB_UNIVERSE": "main"}).strip())
        chk = check_reproduction()
        log(f"reproduction check: {chk}")
        if chk["days"] < 20 or chk["mean_overlap"] < OVERLAP_MIN:
            log(f"STOP: new scores do not reproduce the old ones (mean top-40 overlap < {OVERLAP_MIN}); old scores kept")
            return
        log(f"promoted; old scores archived in {promote()}")
        log(f"scores as of first computation: {archive_asof()}")
        for script in ("forward_book.py", "forward_book_h10.py", "forward_monitor.py", "shadow_x20.py"):
            log(f"{script}: " + run([str(BACKEND_PY), script], env=BOOK_ENV).strip().splitlines()[-1][:600])
        run_os20()
        push_holdings()
        run_llm_live()
        log(f"done in {time.perf_counter() - t0:.0f}s")
    except Exception as e:  # noqa: BLE001 - every failure is logged for the morning check
        log(f"FAILED: {type(e).__name__}: {e}")
        raise
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
