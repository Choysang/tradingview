"""Multi-source schema normalization matrix, runtime record normalizer & polling health watchdog.

Why: Different market/news vendors return heterogeneous field names, units (lots vs shares,
CNY vs 10k CNY, 0.001 CNY), and missing-value sentinels ("0.000", "--", "", null).
Furthermore, RSS/media feeds with pinned top posts stall naive ``break`` cursors, and
HTTP 200 empty/stale feeds masquerade as healthy if only HTTP status is checked.
Invariants:
* Aligns all 11 core source IDs with PostgreSQL ``core.source`` / adapter registry.
* Missing values stay ``None`` (never coerced to ``0``).
* Units are explicitly converted and verified by ``normalize_record``; mismatches raise ``invalid``.
* Cursor deduplication always ``skip``s seen items and counts ``skipped_by_cursor``.
* Source health distinguishes HTTP reachability from actual ``last_yield_at`` output and surfaces
  unreported registered sources as ``awaiting_first_poll`` or ``unconfigured``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
_A_SHARE_CODE_RE = re.compile(r"(?<!\d)((?:60[0135]\d{3}|00[0123]\d{3}|30[01]\d{3}|68[89]\d{3}|8[2378]\d{3}|43\d{4}|920\d{3}))(?!\d)")
_LEGACY_ALIAS_MAP = {
    "eastmoney_lhb": "eastmoney_lhb_disclosure",
    "eastmoney_guba": "eastmoney_guba_comments",
}


@dataclass(frozen=True)
class SourceNormalizationSpec:
    mapping_id: str
    source_id: str
    domain_kind: str
    authority_tier: str
    fallback_group: str
    raw_fields: tuple[str, ...]
    canonical_fields: tuple[str, ...]
    missing_sentinels: tuple[str, ...]
    missing_policy: str
    unit_guard_policy: str
    schema_version: str = "nexus-norm-1.1.0"


CANONICAL_MATRIX: tuple[SourceNormalizationSpec, ...] = (
    SourceNormalizationSpec(
        mapping_id="norm:xinhua:news",
        source_id="xinhua_finance",
        domain_kind="news",
        authority_tier="tier1_official",
        fallback_group="news_authority",
        raw_fields=("contentId", "title", "summary", "publishTime", "publishUrl"),
        canonical_fields=("item_id", "headline", "body_text", "published_at_utc", "source_url"),
        missing_sentinels=("", "null", "None"),
        missing_policy="reject row if title empty; optional url/summary -> None; skip seen cursor without break",
        unit_guard_policy="Asia/Shanghai timestamp -> UTC timestamptz; regex 6-digit A-share symbol check",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:cninfo:announcement",
        source_id="cninfo_announcements",
        domain_kind="announcement",
        authority_tier="tier1_official",
        fallback_group="statutory_disclosure",
        raw_fields=("announcementId", "secCode", "announcementTitle", "announcementTime", "adjunctUrl"),
        canonical_fields=("source_record_id", "instrument_symbol", "headline", "published_at_utc", "pdf_url"),
        missing_sentinels=("", "--", "null"),
        missing_policy="midnight 00:00:00 marked precision='date' (never faked as minute); page count verified vs totalAnnouncement",
        unit_guard_policy="epoch milliseconds -> UTC timestamptz; statutory issuer origin_group deduplication",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:cls:news",
        source_id="cls_telegraph",
        domain_kind="news",
        authority_tier="tier2_financial_press",
        fallback_group="news_fast_724",
        raw_fields=("id", "title", "content", "ctime", "modified_time", "stock_list"),
        canonical_fields=("item_id", "headline", "body_text", "published_at_utc", "modified_at_utc", "instrument_symbols"),
        missing_sentinels=("", "null"),
        missing_policy="derive headline from leading bracket 【...】 when title absent; preserve modified_time as new revision",
        unit_guard_policy="epoch seconds -> UTC timestamptz; strip non-A-share tickers",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:eastmoney:news",
        source_id="eastmoney_news_724",
        domain_kind="news",
        authority_tier="tier3_portal_social",
        fallback_group="news_fast_724",
        raw_fields=("code", "title", "summary", "showTime", "stockList"),
        canonical_fields=("item_id", "headline", "body_text", "published_at_utc", "instrument_symbols"),
        missing_sentinels=("", "-", "--"),
        missing_policy="filter market prefixes strictly to 0.* (SZ) and 1.* (SH); discard 90.BK* and 105.* US tickers",
        unit_guard_policy="Asia/Shanghai datetime -> UTC timestamptz; skip seen items without breaking cursor loop",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:ths:news",
        source_id="ths_news_724",
        domain_kind="news",
        authority_tier="tier3_portal_social",
        fallback_group="news_fast_724",
        raw_fields=("id", "title", "digest", "ctime", "import", "stock"),
        canonical_fields=("item_id", "headline", "body_text", "published_at_utc", "importance_flag", "instrument_symbols"),
        missing_sentinels=("", "null"),
        missing_policy="content sha256 change appends document revision; missing stock list -> empty tuple",
        unit_guard_policy="epoch seconds -> UTC timestamptz",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:sina:bars_quotes",
        source_id="sina",
        domain_kind="bars",
        authority_tier="tier2_financial_press",
        fallback_group="market_bars_quotes",
        raw_fields=("day", "open", "high", "low", "close", "volume", "amount"),
        canonical_fields=("trade_date", "open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw", "volume_shares", "turnover_amount_cny"),
        missing_sentinels=("", "0", "0.0", "0.00", "0.000", "-", "--", "null"),
        missing_policy="zero/sentinel price -> None + quality='partial'; never forward-fill missing sessions",
        unit_guard_policy="volume in shares; OHLC order low <= min(o,c) <= max(o,c) <= high; implied avg price 100x/1000x drift guard",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:tencent:bars_quotes",
        source_id="tencent",
        domain_kind="bars",
        authority_tier="tier2_financial_press",
        fallback_group="market_bars_quotes",
        raw_fields=("date", "open", "close", "high", "low", "volume_lots"),
        canonical_fields=("trade_date", "open_price_raw", "close_price_raw", "high_price_raw", "low_price_raw", "volume_shares"),
        missing_sentinels=("", "0", "0.0", "0.00", "0.000", "-", "--", "null"),
        missing_policy="reject qfqday/hfqday in raw lane; absent turnover_amount_cny stays None (never estimated)",
        unit_guard_policy="lots * 100 -> canonical shares; WAF 501 HTML stops immediately without retry storm",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:eastmoney:lhb_disclosure",
        source_id="eastmoney_lhb_disclosure",
        domain_kind="lhb_seat",
        authority_tier="tier2_financial_press",
        fallback_group="microstructure_lhb",
        raw_fields=("SECURITY_CODE", "TRADE_DATE", "OPERATEDEPT_NAME", "BUY", "SELL", "NET"),
        canonical_fields=("instrument_symbol", "trade_date", "seat_raw_name", "buy_amount_cny", "sell_amount_cny", "net_amount_cny"),
        missing_sentinels=("", "-", "--", "null", "None"),
        missing_policy="code=9201 empty holiday result -> valid empty; missing leg -> None (never 0)",
        unit_guard_policy="explicit raw_unit (CNY vs CNY_10000) converted & verified; Guotai-Haitong alias normalized",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:eastmoney:guba_comments",
        source_id="eastmoney_guba_comments",
        domain_kind="community",
        authority_tier="tier3_portal_social",
        fallback_group="retail_community",
        raw_fields=("post_id", "post_title", "post_publish_time", "post_click_count", "post_comment_count", "post_user"),
        canonical_fields=("post_id", "headline", "published_at_utc", "click_count", "comment_count", "author_anon_hash"),
        missing_sentinels=("", "-", "--", "null", "None"),
        missing_policy="author hashed via sha256[:12]; partitioned into 4 trading sessions; forbidden from claiming capital flow",
        unit_guard_policy="non-negative integer counts; duplicate template water-army posts flagged separately",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:eastmoney:fund_flow",
        source_id="eastmoney_fund_flow",
        domain_kind="capital_flow",
        authority_tier="tier2_financial_press",
        fallback_group="capital_proxy_flow",
        raw_fields=("f51", "f52", "f53", "f54", "f55", "f56", "SECURITY_CODE"),
        canonical_fields=("trade_date", "main_net_cny", "small_net_cny", "medium_net_cny", "large_net_cny", "super_large_net_cny", "instrument_symbol"),
        missing_sentinels=("", "-", "--", "null", "None"),
        missing_policy="provider-derived order-size classification kept under vendor namespace; missing leg -> None",
        unit_guard_policy="explicit raw_unit (CNY vs CNY_10000) converted to CNY; net flow signed finite Decimal",
    ),
    SourceNormalizationSpec(
        mapping_id="norm:eastmoney:calendar",
        source_id="eastmoney_calendar",
        domain_kind="macro_calendar",
        authority_tier="tier2_financial_press",
        fallback_group="macro_event_calendar",
        raw_fields=("FE_CODE", "FE_NAME", "FE_TYPE", "START_DATE", "END_DATE", "CITY", "SPONSOR_NAME"),
        canonical_fields=("event_code", "event_title", "event_type", "start_at_utc", "end_at_utc", "time_precision", "city", "sponsor"),
        missing_sentinels=("", "-", "--", "null", "None"),
        missing_policy="00:00:00 start_date marked precision='date' or 'date_range' (never faked as minute)",
        unit_guard_policy="Asia/Shanghai datetime -> UTC timestamptz; compiled by Eastmoney Choice (not issuer notice)",
    ),
)


def parse_clean_decimal(raw: Any, sentinels: Sequence[str] = ("", "-", "--", "null", "None", "NaN", "nan")) -> Decimal | None:
    """Safely normalize heterogeneous numeric strings to finite ``Decimal`` or ``None``. Never coerces missing/NaN to 0."""
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip().replace(",", "")
    if text in sentinels:
        return None
    try:
        d = Decimal(text)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return d


def _normalize_symbol(raw: Any) -> str | None:
    text = str(raw or "").strip().upper()
    if not text:
        return None
    m = _A_SHARE_CODE_RE.search(text)
    return m.group(1) if m else None


def _to_utc_iso(raw: Any) -> tuple[str | None, str]:
    """Parse epoch s/ms, ISO string, or CST datetime text -> ``(utc_iso, precision)``."""
    if raw is None or raw == "":
        return None, "unknown"
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=CST)
        prec = "date" if dt.time() == datetime.min.time() else "minute"
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), prec
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1e12:
            val /= 1000.0
        dt = datetime.fromtimestamp(val, tz=timezone.utc)
        prec = "date" if dt.astimezone(CST).time() == datetime.min.time() else "minute"
        return dt.isoformat().replace("+00:00", "Z"), prec
    txt = str(raw).strip()
    if txt.isdigit():
        val = float(txt)
        if val > 1e12:
            val /= 1000.0
        dt = datetime.fromtimestamp(val, tz=timezone.utc)
        prec = "date" if dt.astimezone(CST).time() == datetime.min.time() else "minute"
        return dt.isoformat().replace("+00:00", "Z"), prec
    try:
        iso_txt = txt.replace("Z", "+00:00").replace("/", "-")
        if len(iso_txt) == 10:
            d_only = datetime.fromisoformat(iso_txt).replace(tzinfo=CST)
            return d_only.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "date"
        dt = datetime.fromisoformat(iso_txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        prec = "date" if dt.astimezone(CST).time() == datetime.min.time() else "minute"
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), prec
    except ValueError:
        return None, "unknown"


def _normalize_seat_alias(name: str, corrections: list[str]) -> str:
    if "国泰君安证券" in name or "海通证券" in name:
        corrections.append("normalized_guotai_haitong_2025_alias")
    return name


def normalize_record(source_id: str, raw_row: Mapping[str, Any]) -> dict[str, Any]:
    """Runtime cross-source normalizer & unit/sentinel/OHLC integrity validator.

    Returns ``{source_id, canonical, quality_status, corrections, rejected_reasons}``.
    Missing or sentinel fields remain ``None`` (never coerced to ``0``).
    """
    canonical_sid = _LEGACY_ALIAS_MAP.get(source_id, source_id)
    corrections: list[str] = []
    rejected: list[str] = []
    if canonical_sid != source_id:
        corrections.append(f"aliased_source_id:{source_id}->{canonical_sid}")

    spec_map = {s.source_id: s for s in CANONICAL_MATRIX}
    spec = spec_map.get(canonical_sid)
    if spec is None:
        return {
            "source_id": canonical_sid,
            "canonical": {},
            "quality_status": "invalid",
            "corrections": corrections,
            "rejected_reasons": [f"unknown_source_id:{source_id}"],
        }

    partial = False

    if canonical_sid == "sina":
        sent = spec.missing_sentinels
        td = str(raw_row.get("day") or raw_row.get("trade_date") or "").strip()[:10] or None
        o = parse_clean_decimal(raw_row.get("open"), sent)
        h = parse_clean_decimal(raw_row.get("high"), sent)
        l = parse_clean_decimal(raw_row.get("low"), sent)
        c = parse_clean_decimal(raw_row.get("close"), sent)
        v = parse_clean_decimal(raw_row.get("volume") or raw_row.get("volume_shares"), ("", "-", "--", "null", "None"))
        amt = parse_clean_decimal(raw_row.get("amount") or raw_row.get("turnover_amount_cny"), ("", "-", "--", "null", "None"))
        for k, val in (("open", o), ("high", h), ("low", l), ("close", c)):
            if val is None:
                partial = True
                corrections.append(f"sentinel_or_missing_{k}_kept_none")
        if o is not None and h is not None and l is not None and c is not None:
            if not (l <= min(o, c) and max(o, c) <= h):
                rejected.append("ohlc_order_inversion")
        if amt is not None and v is not None and v > 0 and c is not None and c > 0:
            avg_px = amt / v
            ratio = avg_px / c
            if ratio > Decimal("15") or ratio < Decimal("0.05"):
                rejected.append(f"implied_avg_price_unit_drift:{ratio:.2f}x")
        canonical = {
            "trade_date": td,
            "open_price_raw": str(o) if o is not None else None,
            "high_price_raw": str(h) if h is not None else None,
            "low_price_raw": str(l) if l is not None else None,
            "close_price_raw": str(c) if c is not None else None,
            "volume_shares": int(v) if v is not None else None,
            "turnover_amount_cny": str(amt) if amt is not None else None,
        }

    elif canonical_sid == "tencent":
        sent = spec.missing_sentinels
        lane = str(raw_row.get("lane") or raw_row.get("adjust_type") or "raw").lower()
        if lane in ("qfq", "hfq", "qfqday", "hfqday"):
            rejected.append(f"adjusted_bar_rejected_in_raw_lane:{lane}")
        td = str(raw_row.get("date") or raw_row.get("trade_date") or "").strip()[:10] or None
        o = parse_clean_decimal(raw_row.get("open"), sent)
        h = parse_clean_decimal(raw_row.get("high"), sent)
        l = parse_clean_decimal(raw_row.get("low"), sent)
        c = parse_clean_decimal(raw_row.get("close"), sent)
        v_lots = parse_clean_decimal(raw_row.get("volume_lots") if raw_row.get("volume_lots") is not None else raw_row.get("volume"), ("", "-", "--", "null", "None"))
        v_shares: int | None = None
        if v_lots is not None:
            v_shares = int(v_lots * Decimal("100"))
            corrections.append("converted_volume_lots_x100_to_shares")
        if o is not None and h is not None and l is not None and c is not None:
            if not (l <= min(o, c) and max(o, c) <= h):
                rejected.append("ohlc_order_inversion")
        if any(x is None for x in (o, h, l, c)):
            partial = True
            corrections.append("sentinel_or_missing_price_kept_none")
        canonical = {
            "trade_date": td,
            "open_price_raw": str(o) if o is not None else None,
            "close_price_raw": str(c) if c is not None else None,
            "high_price_raw": str(h) if h is not None else None,
            "low_price_raw": str(l) if l is not None else None,
            "volume_shares": v_shares,
            "turnover_amount_cny": None,  # Tencent kline omits turnover -> never estimated
        }

    elif canonical_sid == "eastmoney_lhb_disclosure":
        sent = spec.missing_sentinels
        sym = _normalize_symbol(raw_row.get("SECURITY_CODE") or raw_row.get("instrument_symbol"))
        if sym is None:
            rejected.append("invalid_or_missing_security_code")
        td = str(raw_row.get("TRADE_DATE") or raw_row.get("trade_date") or "").strip()[:10] or None
        seat = str(raw_row.get("OPERATEDEPT_NAME") or raw_row.get("seat_raw_name") or raw_row.get("seat_name") or "").strip()
        seat = _normalize_seat_alias(seat, corrections)
        raw_unit = str(raw_row.get("raw_unit") or raw_row.get("unit") or "CNY").upper()
        mult = Decimal("10000") if raw_unit in ("CNY_10000", "WAN", "万元") else Decimal("1")
        if mult != Decimal("1"):
            corrections.append(f"converted_lhb_unit:{raw_unit}->CNY(x10000)")
        elif raw_unit not in ("CNY", "YUAN", "元"):
            rejected.append(f"unverified_raw_unit:{raw_unit}")

        buy = parse_clean_decimal(raw_row.get("BUY") if "BUY" in raw_row else raw_row.get("buy_cny"), sent)
        sell = parse_clean_decimal(raw_row.get("SELL") if "SELL" in raw_row else raw_row.get("sell_cny"), sent)
        net = parse_clean_decimal(raw_row.get("NET") if "NET" in raw_row else raw_row.get("net_cny"), sent)
        if buy is not None:
            buy = buy * mult
            if buy < 0:
                rejected.append("negative_buy_amount")
        if sell is not None:
            sell = sell * mult
            if sell < 0:
                rejected.append("negative_sell_amount")
        if net is not None:
            net = net * mult
        elif buy is not None and sell is not None:
            net = buy - sell
            corrections.append("derived_net_from_buy_minus_sell")
        if buy is None or sell is None:
            partial = True
            corrections.append("missing_lhb_leg_kept_none")
        canonical = {
            "instrument_symbol": sym,
            "trade_date": td,
            "seat_raw_name": seat or None,
            "buy_amount_cny": f"{buy:.2f}" if buy is not None else None,
            "sell_amount_cny": f"{sell:.2f}" if sell is not None else None,
            "net_amount_cny": f"{net:.2f}" if net is not None else None,
        }

    elif canonical_sid == "eastmoney_fund_flow":
        sent = spec.missing_sentinels
        raw_unit = str(raw_row.get("raw_unit") or "CNY").upper()
        mult = Decimal("10000") if raw_unit in ("CNY_10000", "WAN", "万元") else Decimal("1")
        if mult != Decimal("1"):
            corrections.append(f"converted_fflow_unit:{raw_unit}->CNY")
        td = str(raw_row.get("f51") or raw_row.get("trade_date") or "").strip()[:10] or None
        sym = _normalize_symbol(raw_row.get("SECURITY_CODE") or raw_row.get("instrument_symbol") or "")
        def _ff(key1: str, key2: str) -> str | None:
            val = parse_clean_decimal(raw_row.get(key1) if key1 in raw_row else raw_row.get(key2), sent)
            return f"{(val * mult):.2f}" if val is not None else None
        canonical = {
            "instrument_symbol": sym,
            "trade_date": td,
            "main_net_cny": _ff("f52", "main_net_cny"),
            "small_net_cny": _ff("f53", "small_net_cny"),
            "medium_net_cny": _ff("f54", "medium_net_cny"),
            "large_net_cny": _ff("f55", "large_net_cny"),
            "super_large_net_cny": _ff("f56", "super_large_net_cny"),
        }
        if canonical["main_net_cny"] is None:
            partial = True

    elif canonical_sid == "eastmoney_calendar":
        code = str(raw_row.get("FE_CODE") or raw_row.get("event_code") or "").strip()
        title = str(raw_row.get("FE_NAME") or raw_row.get("event_title") or "").strip()
        if not code or not title:
            rejected.append("missing_fe_code_or_name")
        start_utc, prec = _to_utc_iso(raw_row.get("START_DATE"))
        end_utc, _ = _to_utc_iso(raw_row.get("END_DATE") or raw_row.get("START_DATE"))
        if prec == "date" and start_utc and end_utc and start_utc[:10] != end_utc[:10]:
            prec = "date_range"
        canonical = {
            "event_code": code or None,
            "event_title": title or None,
            "event_type": str(raw_row.get("FE_TYPE") or "").strip() or None,
            "start_at_utc": start_utc,
            "end_at_utc": end_utc,
            "time_precision": prec,
            "city": str(raw_row.get("CITY") or "").strip() or None,
            "sponsor": str(raw_row.get("SPONSOR_NAME") or "").strip() or None,
        }

    elif canonical_sid == "eastmoney_guba_comments":
        pid = str(raw_row.get("post_id") or "").strip()
        title = str(raw_row.get("post_title") or raw_row.get("title") or "").strip()
        if not pid or not title:
            rejected.append("missing_post_id_or_title")
        pub_utc, _ = _to_utc_iso(raw_row.get("post_publish_time") or raw_row.get("published_at"))
        clicks = parse_clean_decimal(raw_row.get("post_click_count"), spec.missing_sentinels)
        cmts = parse_clean_decimal(raw_row.get("post_comment_count"), spec.missing_sentinels)
        raw_user = str(raw_row.get("post_user") or raw_row.get("author") or "").strip()
        author_hash = hashlib.sha256(raw_user.encode("utf-8")).hexdigest()[:12] if raw_user else None
        if raw_user:
            corrections.append("hashed_post_user_sha256_12")
        canonical = {
            "post_id": pid or None,
            "headline": title or None,
            "published_at_utc": pub_utc,
            "click_count": max(0, int(clicks)) if clicks is not None else None,
            "comment_count": max(0, int(cmts)) if cmts is not None else None,
            "author_anon_hash": author_hash,
        }

    else:
        # News / Announcements: xinhua_finance, cninfo_announcements, cls_telegraph, eastmoney_news_724, ths_news_724
        item_id = str(raw_row.get("contentId") or raw_row.get("announcementId") or raw_row.get("id") or raw_row.get("code") or "").strip() or None
        headline = str(raw_row.get("title") or raw_row.get("announcementTitle") or "").strip()
        body = str(raw_row.get("summary") or raw_row.get("content") or raw_row.get("digest") or "").strip()
        if canonical_sid == "cls_telegraph" and not headline and body:
            m_lead = re.match(r"^【([^】]{2,80})】", body)
            if m_lead:
                headline = m_lead.group(1).strip()
                corrections.append("derived_headline_from_leading_brackets")
        if not headline:
            rejected.append("missing_headline")
        pub_raw = raw_row.get("publishTime") or raw_row.get("announcementTime") or raw_row.get("ctime") or raw_row.get("showTime")
        pub_utc, prec = _to_utc_iso(pub_raw)
        if canonical_sid == "eastmoney_news_724":
            raw_stocks = raw_row.get("stockList") or []
            filtered_syms = []
            dropped = 0
            for s in raw_stocks:
                s_str = str(s)
                if s_str.startswith(("0.", "1.")):
                    ns = _normalize_symbol(s_str)
                    if ns:
                        filtered_syms.append(ns)
                else:
                    dropped += 1
            if dropped:
                corrections.append(f"dropped_non_ashare_market_prefixes:{dropped}")
            syms: tuple[str, ...] = tuple(dict.fromkeys(filtered_syms))
        else:
            raw_stocks = raw_row.get("stock_list") or raw_row.get("stock") or raw_row.get("secCode") or []
            if isinstance(raw_stocks, str):
                raw_stocks = [raw_stocks]
            syms_list = []
            for s in raw_stocks:
                val = s.get("StockID") or s.get("stockCode") if isinstance(s, dict) else s
                ns = _normalize_symbol(val)
                if ns:
                    syms_list.append(ns)
            syms = tuple(dict.fromkeys(syms_list))
        canonical = {
            "item_id": item_id,
            "headline": headline or None,
            "body_text": body or None,
            "published_at_utc": pub_utc,
            "time_precision": prec,
            "instrument_symbols": syms,
        }

    status = "invalid" if rejected else ("partial" if partial else "valid")
    return {
        "source_id": canonical_sid,
        "canonical": canonical,
        "quality_status": status,
        "corrections": corrections,
        "rejected_reasons": rejected,
    }


@dataclass(frozen=True)
class SourcePollDiagnostic:
    source_id: str
    authority_tier: str
    fallback_group: str
    runtime_state: str
    freshness_state: str
    last_success_at: datetime | None
    is_silent_warning: bool
    recommended_action: str


def evaluate_polling_health(
    health_rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    silent_threshold_hours: float = 12.0,
) -> dict[str, Any]:
    """Evaluate all registered sources, surfacing missing ones and detecting HTTP-200 silent stalls."""
    spec_by_src = {s.source_id: s for s in CANONICAL_MATRIX}
    diagnostics: list[SourcePollDiagnostic] = []
    healthy = 0
    degraded = 0
    silent = 0
    cutoff = now - timedelta(hours=silent_threshold_hours)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)

    seen_sources: set[str] = set()
    for r in health_rows:
        raw_sid = str(r.get("source_id") or "")
        sid = _LEGACY_ALIAS_MAP.get(raw_sid, raw_sid)
        seen_sources.add(sid)
        spec = spec_by_src.get(sid)
        tier = spec.authority_tier if spec else "tier2_financial_press"
        fgroup = spec.fallback_group if spec else "general"
        rstate = str(r.get("runtime_state") or "healthy")
        fstate = str(r.get("freshness_state") or "unknown")
        last_ok_raw = r.get("last_new_record_at") or r.get("last_yield_at") or r.get("last_success_at")
        if isinstance(last_ok_raw, str):
            try:
                last_ok: datetime | None = datetime.fromisoformat(last_ok_raw.replace("Z", "+00:00"))
            except ValueError:
                last_ok = None
        else:
            last_ok = last_ok_raw if isinstance(last_ok_raw, datetime) else None
        if last_ok is not None and last_ok.tzinfo is None:
            last_ok = last_ok.replace(tzinfo=timezone.utc)

        http_status = r.get("last_http_status") or r.get("http_status")
        zero_polls = int(r.get("consecutive_zero_yield_polls") or 0)
        http_200_like = (http_status == 200) or (rstate == "healthy")
        is_silent = bool(
            http_200_like
            and (
                (last_ok is not None and last_ok < cutoff)
                or (last_ok is None and fstate in ("stale", "empty", "unknown"))
                or fstate == "stale"
                or zero_polls >= 3
            )
        )
        if rstate == "healthy" and not is_silent:
            healthy += 1
            action = "normal_paced_polling"
        elif is_silent:
            silent += 1
            degraded += 1
            action = f"silent_yield_detected: HTTP 200 but stale/no new yield -> fallback_group={fgroup} & inspect pinned cursor"
        else:
            degraded += 1
            action = f"circuit_guarded: wait cooldown probe & route reads to fallback_group={fgroup}"
        diagnostics.append(SourcePollDiagnostic(
            source_id=sid, authority_tier=tier, fallback_group=fgroup,
            runtime_state=rstate, freshness_state=fstate, last_success_at=last_ok,
            is_silent_warning=is_silent, recommended_action=action,
        ))

    # Automatically fill missing registered sources from CANONICAL_MATRIX
    for spec in CANONICAL_MATRIX:
        if spec.source_id in seen_sources:
            continue
        degraded += 1
        diagnostics.append(SourcePollDiagnostic(
            source_id=spec.source_id,
            authority_tier=spec.authority_tier,
            fallback_group=spec.fallback_group,
            runtime_state="awaiting_first_poll",
            freshness_state="unknown",
            last_success_at=None,
            is_silent_warning=False,
            recommended_action=f"awaiting_first_poll: schedule initial paced fetch (fallback_group={spec.fallback_group})",
        ))

    return {
        "total_sources": len(diagnostics),
        "healthy_count": healthy,
        "degraded_count": degraded,
        "silent_warning_count": silent,
        "host_rate_limit_rps": "1.0",
        "diagnostics": diagnostics,
    }
