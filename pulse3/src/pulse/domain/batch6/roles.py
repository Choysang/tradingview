"""Leader / core / follower roles and break / promotion events of E093 (pre-registration section 2).

Why: H1 and H2 need the roles of every Shenwan level-2 industry on every day and the events of the day's leaders. Roles are fixed after the close of D with data of D only.
Definitions (literal):
  * leader: the member with the highest limit-up streak, streak >= 3; ties -> larger D turnover (then smaller code, only to be deterministic);
  * core (zhongjun): excluding the leader, the member with the largest D turnover among members whose float cap is NOT BELOW the median float cap of the industry's
    members (members with a known cap; the median of an even count is the mean of the middle two); no known cap -> not eligible;
  * follower (genfeng): excluding the leader, members that closed limit-up on D with streak == 1 (first board);
  * break event on D: the previous session's leader (streak >= 3) did not close limit-up on D;
  * promotion event on D: the previous session's leader closed limit-up on D (its streak is then >= 4).
Interpretation choices (recorded in section 7 notes): the event leader L is the previous session's leader; the roles of D exclude L and also D's own leader when it
is a different stock; a leader with no bar on D (suspended) gives no event; the previous session must be the immediately preceding session (the caller checks).
Not covered: scope filtering (the caller passes only in-scope members), intraday disagreement, concept lists.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class Member:
    code: str
    streak: int          # consecutive limit-up closes ending on this day (0 = did not close limit-up)
    limit_up: bool
    amount: float        # turnover of the day (estimate, comparable across members)
    cap: float | None    # float cap, None = unknown


@dataclass(frozen=True)
class Event:
    kind: str            # "break" | "promotion"
    leader: str          # the previous session's leader
    leader_streak_prev: int


def leader_of(members: Sequence[Member]) -> Member | None:
    cands = [m for m in members if m.limit_up and m.streak >= 3]
    if not cands:
        return None
    return sorted(cands, key=lambda m: (-m.streak, -m.amount, m.code))[0]


def cap_median(members: Sequence[Member]) -> float | None:
    caps = [m.cap for m in members if m.cap is not None]
    return float(median(caps)) if caps else None


def core_of(members: Sequence[Member], exclude: frozenset[str]) -> Member | None:
    med = cap_median(members)
    if med is None:
        return None
    cands = [m for m in members if m.code not in exclude and m.cap is not None and m.cap >= med]
    if not cands:
        return None
    return sorted(cands, key=lambda m: (-m.amount, m.code))[0]


def followers_of(members: Sequence[Member], exclude: frozenset[str]) -> list[Member]:
    return sorted((m for m in members if m.code not in exclude and m.limit_up and m.streak == 1), key=lambda m: m.code)


def event_of(prev_leader: Member | None, today: Sequence[Member]) -> Event | None:
    """Event of D given the previous session's leader (None when it had none) and D's members; None when no event."""
    if prev_leader is None or prev_leader.streak < 3:
        return None
    me = next((m for m in today if m.code == prev_leader.code), None)
    if me is None:
        return None
    return Event("promotion" if me.limit_up else "break", prev_leader.code, prev_leader.streak)


def targets_with_roles(event: Event, today: Sequence[Member]) -> list[tuple[str, str]]:
    """(code, role) bought after the event: role ``core`` (one at most) then ``follower`` (break events only)."""
    lead_today = leader_of(today)
    exclude = frozenset({event.leader} | ({lead_today.code} if lead_today else set()))
    core = core_of(today, exclude)
    out: list[tuple[str, str]] = [(core.code, "core")] if core else []
    if event.kind == "break":
        out += [(m.code, "follower") for m in followers_of(today, exclude) if m.code != (core.code if core else None)]
    return out


def targets(event: Event, today: Sequence[Member]) -> list[str]:
    """Codes bought after the event: break -> core + followers (H1); promotion -> core only (H2). Duplicates removed."""
    return [c for c, _ in targets_with_roles(event, today)]
