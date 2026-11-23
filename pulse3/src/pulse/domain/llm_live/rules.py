"""E092 design constants, prompt, universe pick, deadline, verdict parsing and cost estimate (pre-registration 2026-11-09, sections 1-3).

Why: the live LLM judgement is a pre-registered forward experiment; every number that binds it (model, caps, deadline, universe) is written once here, with the sha256 of the
frozen pre-registration text, so the code can prove which document it reads. Old behaviour ported from ``scripts/lab/llm_live.py`` and ``llm_ask.py`` (pulse3 does not import ``scripts/``).
Invariants: nothing here is tuned (a changed threshold is a new experiment); the universe pick is deterministic given the day's scores (random controls are seeded with the decision date);
a verdict is accepted only inside its documented range; the cost figure is an ESTIMATE from reported token usage and the configured prices, never a bill.
Not covered: the HTTP call (``adapters.models.responses_client``), persistence and the day loop (``application.bf_llm_live_service``), the judgement arithmetic (``judge``).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from pulse.domain.market_rules.holidays import is_open

EXPERIMENT = "E092"
SPEC_PATH = "docs/research/2026-11-09-e092-llm-live-preregistration.md"
SPEC_SHA256 = "59db7d208f37299874eea5eaa0adbe104d2af6753d792b5f2cd3dfc60b1c9d1f"
FROZEN_END_MARKER = "\n## 4."          # sections 1-3 are frozen; the result is written in a section 4 that is not part of the hash
CST = timezone(timedelta(hours=8))
DEADLINE = time(9, 25)                 # the next session's 09:25: a judgement must be answered before it

LIVE_MODEL, LIVE_EFFORT = "gpt-6.1-terra", "high"
GROUP_TOP, GROUP_CONTROL = "top", "control"
N_TOP, N_CONTROLS = 30, 10
MAX_JOBS, MAX_CALLS, MAX_DECISION_DAYS = 40, 60, 20
MAX_TIMEOUT_RETRIES = 1                # a timeout may be asked once more; nothing else is retried
MAX_CONSECUTIVE_FAILURES = 5           # a day with this many failures in a row stops (no retry storm on a systematic fault)
STOP_HTTP = (401, 403, 429)
ACTIONS = ("buy", "watch", "avoid")
BARS_IN_PROMPT = 20
ENV_DAILY_CAP, DEFAULT_DAILY_CAP_USD = "PULSE_LLM_DAILY_USD_CAP", 5.0
HARD_STOPS = ("http_401", "http_403", "http_429", "refusal", "failures")   # a day stopped by one of these is not resumed without an explicit flag

SYSTEM = """你是一名经验丰富的 A 股交易员，可以联网搜索。今天是 {today}。你要判断一只股票在下一个交易日开盘买入、
持有 5 个交易日后开盘卖出，能否跑赢沪深主板可交易股票的平均收益（往返成本约 0.25%）。
请先搜索：该公司最近两周的公告和新闻、所属行业或题材的最新动态、相关政策，以及当前 A 股整体情绪。
只使用发布日期不晚于今天的信息，并结合下面给出的行情数据作判断。
只输出一个 JSON 对象，不要输出其他文字：
{{"action": "buy" | "watch" | "avoid", "p_outperform": 0-100 的整数, "expected_excess_pct": 数字,
 "reason": "不超过 80 个汉字", "news": [{{"date": "YYYY-MM-DD", "title": "...", "url": "..."}}]}}"""


# ---------------------------------------------------------------------------------------------------------------
# the document that binds the experiment
# ---------------------------------------------------------------------------------------------------------------
def frozen_prefix(raw: bytes) -> bytes:
    """The frozen part of the pre-registration (LF newlines): everything before a "## 4." heading, or the whole file while no result is written."""
    b = raw.replace(b"\r\n", b"\n")
    at = b.find(FROZEN_END_MARKER.encode("utf-8"))
    return b if at < 0 else b[:at].rstrip(b"\n") + b"\n"          # blank lines before the section 4 heading do not change the hash


def frozen_hash(raw: bytes) -> str:
    return hashlib.sha256(frozen_prefix(raw)).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------------------------------------------
def open_day(d: date) -> bool:
    state = is_open(d)
    return d.weekday() < 5 if state is None else state     # a year without a complete schedule falls back to weekdays


def next_open(d: date) -> date:
    d += timedelta(days=1)
    while not open_day(d):
        d += timedelta(days=1)
    return d


def deadline_after(day: date) -> datetime:
    """The next open session's 09:25 (CST): the latest instant at which a judgement of ``day`` may still be asked or counted."""
    return datetime.combine(next_open(day), DEADLINE, CST)


def sessions_after(day: date, n: int) -> date:
    """The n-th open day after ``day`` (the D+n session by the calendar)."""
    d = day
    for _ in range(n):
        d = next_open(d)
    return d


# ---------------------------------------------------------------------------------------------------------------
# universe and prompt
# ---------------------------------------------------------------------------------------------------------------
def pick_universe(scores: Mapping[str, float], day: date, n_top: int = N_TOP, n_controls: int = N_CONTROLS) -> tuple[list[str], list[str]]:
    """(top codes in rank order, control codes): the n_top best scores (ties by code), and n_controls random others (``random.Random(day.toordinal())`` over the sorted rest)."""
    finite = {c: s for c, s in scores.items() if s is not None and math.isfinite(s)}
    if len(finite) < n_top + n_controls:
        raise ValueError(f"{day}: only {len(finite)} scored names for {n_top} + {n_controls}")
    ranked = sorted(finite, key=lambda c: (-finite[c], c))
    top = ranked[:n_top]
    rest = sorted(set(finite) - set(top))
    return top, random.Random(day.toordinal()).sample(rest, n_controls)


def system_text(day: date) -> str:
    return SYSTEM.format(today=day.isoformat())


def bars_text(bars: Sequence[Mapping]) -> str:
    """The last 20 bars, one line each: date, close, change, turnover (amount / float cap, both in 100 million CNY) and the limit flag. An unknown turnover is shown as n/a."""
    out = []
    for r in list(bars)[-BARS_IN_PROMPT:]:
        cap, amt = r.get("float_cap"), r.get("amount")
        turn = "n/a" if not cap or amt is None or not math.isfinite(cap) or not math.isfinite(amt) else f"{amt / cap * 100:.2f}%"
        flag = "涨停" if r.get("is_limit_up") else "跌停" if r.get("is_limit_down") else ""
        pct = r.get("pct_chg")
        change = "n/a" if pct is None or not math.isfinite(pct) else f"{pct:+.2f}%"
        out.append(f"{r['date']} 收{r['close']:.2f} {change} 换手{turn} {flag}".rstrip())
    return "\n".join(out)


def build_prompt(*, market: str, code: str, name: str, industry: str | None, float_cap: float | None, bars: Sequence[Mapping]) -> str:
    ind = f"行业 {industry}，" if industry else ""                 # some names have no industry: the line then omits it
    cap = f"流通市值约 {float_cap:.0f} 亿元" if float_cap is not None and math.isfinite(float_cap) else "流通市值未知"
    return f"市场：{market}\n股票：{code} {name}，{ind}{cap}\n近20个交易日：\n{bars_text(bars)}"


def market_line(day: date, closes: Sequence[float]) -> str:
    """Index line of the prompt from the index closes up to ``day`` (last element = ``day``, at least 61 values)."""
    if len(closes) < 61 or not all(map(math.isfinite, (closes[-1], closes[-6], closes[-21], closes[-61]))):
        raise ValueError("the index series is too short or has a gap for the market line")
    c = closes
    return (f"上证指数 {day} 收盘 {c[-1]:.2f}，近5日 {(c[-1] / c[-6] - 1) * 100:+.1f}%，近20日 {(c[-1] / c[-21] - 1) * 100:+.1f}%，"
            f"近60日 {(c[-1] / c[-61] - 1) * 100:+.1f}%")


def prompt_sha256(system: str, prompt: str) -> str:
    return hashlib.sha256((system + "\n" + prompt).encode("utf-8")).hexdigest()


def job_id(day: date, code: str) -> str:
    return f"{day.isoformat()}_{code}"


# ---------------------------------------------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------------------------------------------
def parse_verdict(text: str) -> dict:
    """The JSON object of a reply: action in buy/watch/avoid and p_outperform a number in [0, 100] (a bool is not a number). Raises ValueError otherwise."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    obj = json.loads(text[start: end + 1])
    p = obj.get("p_outperform")
    if obj.get("action") not in ACTIONS or isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 100:
        raise ValueError("verdict fields out of range")
    e = obj.get("expected_excess_pct")
    if e is not None and (isinstance(e, bool) or not isinstance(e, (int, float)) or not math.isfinite(e)):
        raise ValueError("expected_excess_pct is not a number")
    return obj


# ---------------------------------------------------------------------------------------------------------------
# cost (an estimate from token usage; the real bill is the vendor's)
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Prices:
    """USD per million input tokens, per million output tokens (reasoning tokens are part of the output), per web-search call, and the amount assumed for a call whose cost is unknown."""
    in_per_m: float = 5.0
    out_per_m: float = 25.0
    search_per_call: float = 0.01
    reserve_per_call: float = 0.25


PRICE_ENV = {"in_per_m": "PULSE_LLM_PRICE_IN_PER_M", "out_per_m": "PULSE_LLM_PRICE_OUT_PER_M", "search_per_call": "PULSE_LLM_PRICE_SEARCH_PER_CALL",
             "reserve_per_call": "PULSE_LLM_CALL_RESERVE_USD"}


def _positive(name: str, raw: str) -> float:
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"{name} is not a number") from None
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return v


def prices_from_env(env: Mapping[str, str] | None = None) -> Prices:
    env = os.environ if env is None else env
    base = Prices()
    return Prices(**{f: _positive(var, env[var]) if env.get(var, "").strip() else getattr(base, f) for f, var in PRICE_ENV.items()})


def daily_cap_from_env(env: Mapping[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    raw = env.get(ENV_DAILY_CAP, "").strip()
    return _positive(ENV_DAILY_CAP, raw) if raw else DEFAULT_DAILY_CAP_USD


def estimate_cost(usage: Mapping | None, searches: int, prices: Prices) -> float | None:
    """USD estimate, or None when the reply reported no usable token counts (the caller then assumes ``reserve_per_call``). Cached input is charged at the full rate (conservative)."""
    if not usage:
        return None
    tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
    if not isinstance(tin, (int, float)) or not isinstance(tout, (int, float)) or isinstance(tin, bool) or isinstance(tout, bool):
        return None
    return round(tin / 1e6 * prices.in_per_m + tout / 1e6 * prices.out_per_m + searches * prices.search_per_call, 6)


def _known_costs(rows: Sequence[Mapping], day: str) -> list[float]:
    return [r["body"]["cost_usd"] for r in rows if r["session"] == day and r["kind"] in ("answer", "error") and r["body"].get("cost_usd") is not None]


def unit_cost(rows: Sequence[Mapping], day: str, prices: Prices) -> float:
    """What a call without a known cost is counted at: the larger of the reserve and today's mean known cost per paid call (zero-cost refusals are not paid calls)."""
    paid = [c for c in _known_costs(rows, day) if c > 0]
    return max(prices.reserve_per_call, sum(paid) / len(paid)) if paid else prices.reserve_per_call


def day_spend(rows: Sequence[Mapping], day: str, prices: Prices) -> tuple[float, int]:
    """(estimated USD spent on ``day``, calls counted at the unit cost). A call with no result row (in flight or lost) or a result without a known cost counts ``unit_cost``."""
    seen: set[int] = set()
    unknown = 0
    for r in rows:
        if r["session"] != day or r["kind"] not in ("answer", "error"):
            continue
        n = r["body"].get("call_n")
        if n is not None:
            seen.add(n)
        if r["body"].get("cost_usd") is None:
            unknown += 1
    calls = {r["body"]["n"] for r in rows if r["session"] == day and r["kind"] == "call"}
    unresolved = len(calls - seen) + unknown
    return sum(_known_costs(rows, day)) + unresolved * unit_cost(rows, day, prices), unresolved


def allows_next_call(rows: Sequence[Mapping], day: str, prices: Prices, cap_usd: float) -> bool:
    """True while the estimated spend plus one more call (at the unit cost) stays within the cap."""
    spent, _ = day_spend(rows, day, prices)
    return spent + unit_cost(rows, day, prices) <= cap_usd


# a failure that reports no usage costs nothing only when the vendor refused before doing any work; every other failure after the request was sent counts at the unit cost
ZERO_COST_LABELS = ("http_401", "http_403", "http_429", "refusal")


def failure_cost(label: str, usage_cost: float | None) -> float | None:
    """Cost to record for a failed call: the usage estimate when the reply reported usage; 0.0 for a refusal that reports none; None (counted at the unit cost) for anything else."""
    if usage_cost is not None:
        return usage_cost
    return 0.0 if label in ZERO_COST_LABELS else None


# ---------------------------------------------------------------------------------------------------------------
# output budget
# ---------------------------------------------------------------------------------------------------------------
ENV_MAX_OUTPUT_TOKENS = "PULSE_LLM_MAX_OUTPUT_TOKENS"
#: The answer is one JSON object of about 300-500 tokens (80 characters of reason, a few news items); the old lab's no-search calls used about 200 reasoning tokens, and a web-searching
#: call at effort high plausibly reasons for a few thousand. The limit also counts reasoning tokens, so 12000 leaves wide headroom (a truncated reply would be a wasted paid call) while capping
#: one call at about 0.30 USD at the default output price.
DEFAULT_MAX_OUTPUT_TOKENS = 12000
MIN_MAX_OUTPUT_TOKENS = 1000


def max_output_tokens_from_env(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = env.get(ENV_MAX_OUTPUT_TOKENS, "").strip()
    if not raw:
        return DEFAULT_MAX_OUTPUT_TOKENS
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"{ENV_MAX_OUTPUT_TOKENS} is not an integer") from None
    if v < MIN_MAX_OUTPUT_TOKENS:
        raise ValueError(f"{ENV_MAX_OUTPUT_TOKENS} must be at least {MIN_MAX_OUTPUT_TOKENS}")
    return v
