"""Auction features (E095, section 4), active buy ratio (E096, section 5) and the field check of the auction quote fields.

Why: H5 / H6 / H7 are forward tests on the recorded 60 s snapshots. This module holds the pure arithmetic: features from the chosen snapshots, the day's selections,
and the counters that decide, from the recorded data itself, what bid1 / ask1 / bid2 lots mean between 09:15 and 09:25.
Field check (evidence written to section 7): during 09:15-09:24 (kinds ``auction`` / ``auction_hot``) the vendor shows the INDICATIVE match: ``price`` is the indicative
price, bid1 and ask1 price both equal it and bid1 lots == ask1 lots == the indicative matched quantity; bid2 lots / ask2 lots (no price) are the UNMATCHED surplus on the
buy / sell side (never both positive). At the final 09:25 snapshot (kind ``auction_final``) ``volume_lots`` / ``amount_yuan`` are the matched totals and bid / ask 1..5 are the
resting book AFTER matching, i.e. unmatched orders. The rule that decides is ``conclude``; if a criterion fails the dependent feature is "not computable".
Definitions: ``gap_925`` = price of the last snapshot polled 09:25:00-09:29:59 over the previous close minus 1 (needs matched volume > 0); ``gap_919`` = price of the last snapshot
polled before 09:20:00 (and not before 09:15) over previous close minus 1; ``fade`` = gap_919 - gap_925; ``amt_925_ratio`` = matched amount at 09:25 / mean daily turnover of the
previous 20 sessions; ``unmatched_buy`` = sum of bid1..bid5 lots of the 09:25 snapshot (lots, lower bound of the unmatched buy: only five levels are published), computed only when the
field check passes; ``matched_915`` = bid1 lots of the first snapshot (the 09:15 matched quantity; recorded only, not a hypothesis). ``abr_30`` = d outer / (d outer + d inner)
between the first snapshot polled at or after 09:30:00 and the first at or after 10:00:00 (each within 3 minutes); not computable when the sum of the increments is not positive.
Selections: H5 gap_925 in [+2 %, +7 %] and amt_925_ratio in the top 20 % of the day's names that have the ratio; H6 gap_919 >= +5 % and fade >= 3 percentage points; H7 10:00 change in [0, +5 %]
and abr_30 in the top 10 % of the day's names that have it, buy at the 10:00 snapshot ask1 (ask1 lots must be > 0), sell at the close.
Not covered: queueing, intraday disagreement, selection thresholds other than the pre-registered ones.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median

from pulse.domain.batch6.evaluate import percent_rank_top

GAP_925_LO, GAP_925_HI = 0.02, 0.07
AMT_TOP_SHARE = 0.20
H6_GAP_919, H6_FADE = 0.05, 0.03
H7_CHG_LO, H7_CHG_HI = 0.0, 0.05
ABR_TOP_SHARE = 0.10
FIELD_MIN_SHARE = 0.99                       # share of rows that must satisfy a field criterion
FIELD_RULE_VERSION = "fieldcheck-v2"         # v1 counted rows without an indicative book (bid1 / ask1 price missing) as failures: a bug of the check, fixed before the forward start


def gap(price: float | None, prev_close: float | None) -> float | None:
    if price is None or prev_close is None or price <= 0 or prev_close <= 0:
        return None
    return price / prev_close - 1.0


@dataclass(frozen=True)
class AuctionFeatures:
    gap_919: float | None
    gap_925: float | None
    fade: float | None
    amt_925_ratio: float | None
    unmatched_buy: float | None
    matched_915: float | None


def features(*, prev_close: float | None, price_919: float | None, price_925: float | None, matched_volume_925: float | None, amount_925: float | None,
             avg_amount_20: float | None, bid_lots_5_925: float | None, matched_915: float | None, unmatched_ok: bool) -> AuctionFeatures:
    g919 = gap(price_919, prev_close)
    matched = matched_volume_925 is not None and matched_volume_925 > 0
    g925 = gap(price_925, prev_close) if matched else None
    fade = g919 - g925 if g919 is not None and g925 is not None else None
    ratio = amount_925 / avg_amount_20 if matched and amount_925 is not None and avg_amount_20 and avg_amount_20 > 0 else None
    return AuctionFeatures(g919, g925, fade, ratio, bid_lots_5_925 if unmatched_ok else None, matched_915)


def h5_signals(feats: Mapping[str, AuctionFeatures]) -> list[str]:
    ratios = {c: f.amt_925_ratio for c, f in feats.items() if f.amt_925_ratio is not None}
    top = percent_rank_top(ratios, AMT_TOP_SHARE)
    return sorted(c for c in top if feats[c].gap_925 is not None and GAP_925_LO <= feats[c].gap_925 <= GAP_925_HI)


def h6_signals(feats: Mapping[str, AuctionFeatures]) -> list[str]:
    return sorted(c for c, f in feats.items() if f.gap_919 is not None and f.fade is not None and f.gap_919 >= H6_GAP_919 and f.fade >= H6_FADE - 1e-12)


def abr(outer_start: float | None, inner_start: float | None, outer_end: float | None, inner_end: float | None) -> float | None:
    if None in (outer_start, inner_start, outer_end, inner_end):
        return None
    do, di = outer_end - outer_start, inner_end - inner_start
    if do < 0 or di < 0 or do + di <= 0:
        return None
    return do / (do + di)


def h7_signals(change_10: Mapping[str, float | None], abr_30: Mapping[str, float | None], ask1_lots_10: Mapping[str, float | None]) -> list[str]:
    ratios = {c: v for c, v in abr_30.items() if v is not None}
    top = percent_rank_top(ratios, ABR_TOP_SHARE)
    out = []
    for c in top:
        ch = change_10.get(c)
        if ch is not None and H7_CHG_LO <= ch <= H7_CHG_HI:
            lots = ask1_lots_10.get(c)
            if lots is not None and lots > 0:
                out.append(c)
    return sorted(out)


# ---- field check ----------------------------------------------------------------------------------------------------------------------
@dataclass
class FieldCounts:
    auction_rows: int = 0            # 09:15-09:24 rows with a price AND an indicative book (bid1 and ask1 price present)
    no_indication: int = 0           # 09:15-09:24 rows with a price but no bid1 / ask1 price (nothing crosses): not evidence either way
    bid1_eq_ask1_lots: int = 0
    px_all_equal: int = 0            # bid1 px == ask1 px == price
    bid2_ask2_both_pos: int = 0
    bid2_or_ask2_pos: int = 0
    final_rows: int = 0              # 09:25 final rows with matched volume > 0
    final_amount_consistent: int = 0 # |amount - lots * 100 * price| <= 0.1 % of amount
    final_uncrossed: int = 0         # bid1 px < ask1 px (both present)
    final_book_rows: int = 0         # rows with both bid1 and ask1 present
    same_price_rows: int = 0         # final price equal to the last indicative price
    same_price_ge: int = 0           # ... and the final matched volume is at least the last indicative matched quantity (no cancel after 09:20: it can only grow)
    ratio_final_vs_last_auction: list[float] = None  # type: ignore[assignment]  # matched volume final / last indicative matched volume, where the price did not change

    def __post_init__(self) -> None:
        if self.ratio_final_vs_last_auction is None:
            self.ratio_final_vs_last_auction = []

    def merge(self, other: "FieldCounts") -> None:
        for k in ("auction_rows", "no_indication", "same_price_rows", "same_price_ge", "bid1_eq_ask1_lots", "px_all_equal", "bid2_ask2_both_pos", "bid2_or_ask2_pos", "final_rows", "final_amount_consistent",
                  "final_uncrossed", "final_book_rows"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.ratio_final_vs_last_auction += other.ratio_final_vs_last_auction


def count_auction_row(c: FieldCounts, *, price, bid1_px, ask1_px, bid1_lots, ask1_lots, bid2_lots, ask2_lots) -> None:
    if price is None or price <= 0:
        return
    if bid1_px is None or ask1_px is None:
        c.no_indication += 1
        return
    c.auction_rows += 1
    c.bid1_eq_ask1_lots += int(bid1_lots is not None and ask1_lots is not None and abs(bid1_lots - ask1_lots) < 1e-9)
    c.px_all_equal += int(bid1_px is not None and ask1_px is not None and abs(bid1_px - price) < 1e-6 and abs(ask1_px - price) < 1e-6)
    b2, a2 = (bid2_lots or 0.0) > 0, (ask2_lots or 0.0) > 0
    c.bid2_ask2_both_pos += int(b2 and a2)
    c.bid2_or_ask2_pos += int(b2 or a2)


def count_final_row(c: FieldCounts, *, price, volume_lots, amount, bid1_px, ask1_px, last_auction_price, last_auction_matched) -> None:
    if price is None or price <= 0 or volume_lots is None or volume_lots <= 0:
        return
    c.final_rows += 1
    if amount is not None and amount > 0:
        c.final_amount_consistent += int(abs(amount - volume_lots * 100.0 * price) <= 0.001 * amount)
    if bid1_px is not None and ask1_px is not None and bid1_px > 0 and ask1_px > 0:
        c.final_book_rows += 1
        c.final_uncrossed += int(bid1_px < ask1_px)
    if last_auction_price is not None and last_auction_matched and last_auction_matched > 0 and abs(last_auction_price - price) < 1e-6:
        c.ratio_final_vs_last_auction.append(volume_lots / last_auction_matched)
        c.same_price_rows += 1
        c.same_price_ge += int(volume_lots >= last_auction_matched * 0.999)


def _share(a: int, b: int) -> float | None:
    return None if b == 0 else a / b


def conclude(c: FieldCounts) -> dict:
    """Evidence summary and the decision: which fields are what, and whether ``unmatched_buy`` is computable."""
    s_eq = _share(c.bid1_eq_ask1_lots, c.auction_rows)
    s_px = _share(c.px_all_equal, c.auction_rows)
    s_both = _share(c.bid2_ask2_both_pos, c.bid2_or_ask2_pos)
    s_amt = _share(c.final_amount_consistent, c.final_rows)
    s_unc = _share(c.final_uncrossed, c.final_book_rows)
    med = float(median(c.ratio_final_vs_last_auction)) if c.ratio_final_vs_last_auction else None
    indicative = bool(s_eq is not None and s_eq >= FIELD_MIN_SHARE and s_px is not None and s_px >= FIELD_MIN_SHARE)
    surplus = bool(s_both is not None and s_both <= 1 - FIELD_MIN_SHARE and c.bid2_or_ask2_pos > 0)
    final_ok = bool(s_amt is not None and s_amt >= FIELD_MIN_SHARE and s_unc is not None and s_unc >= FIELD_MIN_SHARE)
    return {
        "rule_version": FIELD_RULE_VERSION, "auction_rows": c.auction_rows, "rows_without_indicative_book": c.no_indication, "share_bid1_lots_eq_ask1_lots": s_eq, "share_bid1_ask1_px_eq_price": s_px,
        "rows_with_bid2_or_ask2": c.bid2_or_ask2_pos, "share_bid2_and_ask2_both_positive": s_both,
        "final_rows": c.final_rows, "share_final_amount_eq_lots_x_price": s_amt, "share_final_book_uncrossed": s_unc,
        "median_final_over_last_indicative_matched_volume_same_price": med, "n_same_price": len(c.ratio_final_vs_last_auction),
        "share_final_volume_at_least_last_indicative_same_price": _share(c.same_price_ge, c.same_price_rows),
        "bid1_ask1_lots_0915_0924": "indicative matched quantity (matched, not unmatched)" if indicative else "undetermined",
        "bid2_ask2_lots_0915_0924": "unmatched surplus on the buy (bid2) / sell (ask2) side, mutually exclusive" if surplus else "undetermined",
        "bid_ask_book_at_0925": "resting orders after matching = unmatched" if final_ok else "undetermined",
        "unmatched_buy_computable": bool(final_ok),
        "unmatched_buy_definition": "sum of bid1..bid5 lots of the 09:25 snapshot (lower bound; five levels published)" if final_ok else "not computable",
        "thresholds": {"share_min": FIELD_MIN_SHARE},
    }
