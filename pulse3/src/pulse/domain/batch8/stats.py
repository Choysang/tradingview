"""E099 statistics: H8a (next-day exit) and H8b (entry filter) on per-name records, plus the reports that never decide.

Why: pre-registration sections 1, 3, 4. History, forward judge and tests all call these functions on the same ``NameRec`` rows, so a number is computed one way only.
Statistics (matched excess = return minus the same-day style-matched baseline mean, E062; block bootstrap, E082):
  * H8a: FLAG names with a filled D+1 open -> D+5 close leg that is NOT gated by the entry rule (the position is already held, so an open at the limit-up price is no reason to drop
    it) and has a matched baseline. effect = name-weighted mean excess; one-sided p from the block bootstrap of ``-excess`` (block 5, seed 20261123, 10,000 draws, slots = every resolved
    session) i.e. "flagged names are not worse" is the null. Cost: the baseline pays the same round trip, so the excess is the same before and after cost; ``mean_ret_net`` is reported.
  * H8b: the entry arm: legs filled under the entry rule (D+1 open at the limit-up price = not bought, in BOTH groups). Per session the mean excess of the FLAG names minus the mean
    excess of the unflagged names, only for sessions where both groups have a matched name. effect = mean of those differences (session weighted, as the document defines it); p from
    the block bootstrap of ``-difference`` with one unit of weight per eligible session.
  * Reports (never decide): the same two statistics for first-board / consecutive-board names, and with L3, L1, T1 alone as the flagged arm; fill rates of the entry arm per group.
Not covered: building the records (``bf_batch8_core``), the sample-size rule and Holm (``rules``).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from pulse.domain.batch8 import rules as R
from pulse.domain.research import bootstrap as BS


@dataclass(frozen=True)
class NameRec:
    """One limit-up-close name of a decision day with its flags and both D+1 -> D+5 legs (``*_ex`` = matched excess, None when the leg is not filled or has no baseline)."""
    day: str
    code: str
    group: str                    # first | consecutive | unknown_prev
    l1: bool
    l3: bool
    t1: bool
    flag: bool
    held_ex: float | None         # H8a leg (not gated by the entry rule)
    held_ret: float | None
    gated_status: str             # entry-rule leg status: filled | blocked_buy | no_bar | flag_unknown | exit_unresolved
    gated_ex: float | None
    gated_ret: float | None


@dataclass(frozen=True)
class Effect:
    effect: float | None
    p_value: float | None
    n_arm: int                    # matched names of the flagged arm (all sessions)
    n_rest: int                   # matched names of the other side (H8b only)
    n_eligible: int               # sessions that carry the statistic (H8a: sessions with a flagged matched name; H8b: sessions with both groups)
    blocks: int
    reliable: bool
    mean_ret_net: float | None = None
    note: str | None = None

    def wire(self) -> dict:
        return {k: getattr(self, k) for k in ("effect", "p_value", "n_arm", "n_rest", "n_eligible", "blocks", "reliable", "mean_ret_net", "note")}


Arm = Callable[[NameRec], bool]


def _day_means(values: Iterable[tuple[str, float]]) -> dict[str, tuple[int, float]]:
    acc: dict[str, list[float]] = defaultdict(list)
    for d, v in values:
        acc[d].append(v)
    return {d: (len(v), sum(v) / len(v)) for d, v in acc.items()}


def h8a(recs: Sequence[NameRec], all_days: Sequence[str], arm: Arm = lambda r: r.flag) -> Effect:
    """H8a on the names selected by ``arm`` (default FLAG)."""
    sel = [r for r in recs if arm(r) and r.held_ex is not None]
    if not sel:
        return Effect(None, None, 0, 0, 0, 0, False, None, "no_matched_names")
    per = _day_means((r.day, r.held_ex) for r in sel)
    days = sorted(per)
    n = sum(per[d][0] for d in days)
    effect = sum(per[d][0] * per[d][1] for d in days) / n
    bs = BS.block_bootstrap_p(list(all_days), days, [R.SIGN * per[d][1] for d in days], [per[d][0] for d in days], block=R.HOLD, seed=R.SEED, n_boot=R.N_BOOT)
    rets = [r.held_ret for r in sel if r.held_ret is not None]
    net = (sum(rets) / len(rets) - float(R.COST)) if rets else None
    return Effect(effect, bs.p_value, n, 0, len(days), bs.blocks, bs.reliable, net, bs.reason)


def h8b(recs: Sequence[NameRec], all_days: Sequence[str], arm: Arm = lambda r: r.flag) -> Effect:
    """H8b: (``arm`` mean - rest mean) of the entry-rule excess per session, sessions where both sides have a matched name."""
    a = _day_means((r.day, r.gated_ex) for r in recs if arm(r) and r.gated_ex is not None)
    b = _day_means((r.day, r.gated_ex) for r in recs if not arm(r) and r.gated_ex is not None)
    days = sorted(set(a) & set(b))
    n_a, n_b = sum(v[0] for v in a.values()), sum(v[0] for v in b.values())
    if not days:
        return Effect(None, None, n_a, n_b, 0, 0, False, None, "no_session_with_both_groups")
    diff = [a[d][1] - b[d][1] for d in days]
    effect = sum(diff) / len(diff)
    bs = BS.block_bootstrap_p(list(all_days), days, [R.SIGN * x for x in diff], [1] * len(days), block=R.HOLD, seed=R.SEED, n_boot=R.N_BOOT)
    return Effect(effect, bs.p_value, n_a, n_b, len(days), bs.blocks, bs.reliable, None, bs.reason)


def fill_rates(recs: Sequence[NameRec]) -> dict:
    """Entry-arm fill rate (filled / (filled + blocked_buy)) and status counts for FLAG and unflagged names."""
    out = {}
    for label, sel in (("flag", [r for r in recs if r.flag]), ("noflag", [r for r in recs if not r.flag])):
        cnt: dict[str, int] = defaultdict(int)
        for r in sel:
            cnt[r.gated_status] += 1
        denom = cnt.get("filled", 0) + cnt.get("blocked_buy", 0)
        out[label] = {"names": len(sel), "status": dict(cnt), "fill_rate": (cnt.get("filled", 0) / denom) if denom else None}
    return out


def pair(recs: Sequence[NameRec], all_days: Sequence[str], arm: Arm = lambda r: r.flag) -> dict:
    return {"H8a": h8a(recs, all_days, arm).wire(), "H8b": h8b(recs, all_days, arm).wire(), "names": len(recs), "arm_names": sum(1 for r in recs if arm(r))}


def report(recs: Sequence[NameRec], all_days: Sequence[str]) -> dict:
    """Judged pair on the whole sample (``whole``) and the reports that never decide."""
    whole = pair(recs, all_days)
    whole["fill"] = fill_rates(recs)
    out = {"whole": whole, "by_group": {}, "by_component": {}}
    for g in ("first", "consecutive", "unknown_prev"):
        sub = [r for r in recs if r.group == g]
        if sub:
            out["by_group"][g] = {**pair(sub, all_days), "fill": fill_rates(sub)}
    for name, arm in (("L3", lambda r: r.l3), ("L1", lambda r: r.l1), ("T1", lambda r: r.t1)):
        out["by_component"][name] = pair(recs, all_days, arm)
    return out


def flag_samples_per_day(recs: Sequence[NameRec], days: Sequence[str]) -> list[int]:
    """FLAG samples (H8a matched) of each session in ``days`` order (the sample-size input of ``rules.sample_cut``)."""
    c: dict[str, int] = defaultdict(int)
    for r in recs:
        if r.flag and r.held_ex is not None:
            c[r.day] += 1
    return [c.get(d, 0) for d in days]
