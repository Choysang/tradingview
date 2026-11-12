"""Stable instrument identity, aliases, board and risk-warning state (spec FINAL §4, RECON §4).

Why: ``600000.SH`` style codes are display aliases with validity, not identity.
The identity is a deterministic UUIDv5 over a fixed namespace plus the exchange
MIC and the exchange-issued code, so every worker derives the same id without a
registry round trip. Alias rows (source + source symbol + effective interval)
resolve what each source calls the instrument.

Invariants: the namespace constant never changes; the exchange for a *bare*
code is decided once at instrument registration from the exchange's published
numbering ranges (``venue_for_code``) and afterwards only alias lookup resolves
identity (never a prefix guess inside normalization); ST state is parsed from
the display name of that dated snapshot only and never back-filled.
Not covered: B-shares, funds, bonds (asset types beyond A-share equity/index).
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date

NAMESPACE = uuid.UUID("5f0c4e1a-9d3b-4a6e-8c27-7b1d2e9a4f10")  # fixed forever: changing it re-identifies every instrument

XSHG, XSHE, XBSE = "XSHG", "XSHE", "XBSE"
SUFFIX = {XSHG: "SH", XSHE: "SZ", XBSE: "BJ"}
SUFFIX_TO_VENUE = {v: k for k, v in SUFFIX.items()}
SINA_PREFIX = {XSHG: "sh", XSHE: "sz", XBSE: "bj"}

BOARD_MAIN, BOARD_CHINEXT, BOARD_STAR, BOARD_BSE = "main", "chinext", "star", "bse"


def instrument_uuid(venue: str, code: str, instrument_type: str = "equity") -> uuid.UUID:
    if venue not in SUFFIX:
        raise ValueError(f"unknown venue {venue!r}")
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"code must be 6 digits with leading zeros preserved: {code!r}")
    return uuid.uuid5(NAMESPACE, f"{instrument_type}:{venue}:{code}")


def venue_for_code(code: str) -> str:
    """Exchange from the published A-share numbering ranges (registration-time only)."""
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"bad code {code!r}")
    if code[0] == "6":
        return XSHG
    if code[0] in "03":
        return XSHE
    if code[0] in "489":
        return XBSE
    raise ValueError(f"code {code} is outside the supported A-share ranges")


def board_for(venue: str, code: str) -> str:
    """Board classification by exchange numbering rules."""
    if venue == XSHG:
        if code.startswith(("688", "689")):
            return BOARD_STAR
        if code.startswith(("600", "601", "603", "605")):
            return BOARD_MAIN
    elif venue == XSHE:
        if code.startswith(("300", "301")):
            return BOARD_CHINEXT
        if code.startswith(("000", "001", "002", "003")):
            return BOARD_MAIN
    elif venue == XBSE:
        return BOARD_BSE
    raise ValueError(f"cannot classify {venue}:{code}")


_ST = re.compile(r"^(?:\*ST|S\*ST|SST|ST)(?![A-Za-z])")


def parse_display_name(name: str) -> tuple[str, bool]:
    """(clean_name, is_risk_warning). Full-width blanks and letters are normalized (NFKC)."""
    clean = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name))
    return clean, bool(_ST.match(clean))


def parse_symbol(text: str) -> tuple[str, str]:
    """Parse ``600000.SH`` / ``sh600000`` / ``SZ000001`` into (code, venue); bare codes are refused."""
    t = text.strip()
    m = re.fullmatch(r"(\d{6})\.([A-Za-z]{2})", t)
    if m and m.group(2).upper() in SUFFIX_TO_VENUE:
        return m.group(1), SUFFIX_TO_VENUE[m.group(2).upper()]
    m = re.fullmatch(r"([A-Za-z]{2})(\d{6})", t)
    if m and m.group(1).upper() in SUFFIX_TO_VENUE:
        return m.group(2), SUFFIX_TO_VENUE[m.group(1).upper()]
    raise ValueError(f"symbol {text!r} needs an explicit market suffix/prefix")


def display_symbol(venue: str, code: str) -> str:
    return f"{code}.{SUFFIX[venue]}"


@dataclass(frozen=True)
class InstrumentRecord:
    instrument_id: uuid.UUID
    venue_code: str
    code: str
    instrument_type: str
    display_name: str
    board_code: str
    is_risk_warning: bool
    listed_on: date | None = None

    @staticmethod
    def build(code: str, name: str, listed_on: date | None = None, instrument_type: str = "equity") -> "InstrumentRecord":
        venue = venue_for_code(code)
        clean, st = parse_display_name(name)
        return InstrumentRecord(instrument_uuid(venue, code, instrument_type), venue, code, instrument_type, clean,
                                board_for(venue, code), st, listed_on)

    @property
    def symbols(self) -> dict[str, str]:
        """Aliases by source id for the sources this package reads."""
        pfx = SINA_PREFIX[self.venue_code]
        return {
            "legacy_warehouse": self.code,
            "sina": f"{pfx}{self.code}",
            "tencent": f"{pfx}{self.code}",
            "cninfo_announcements": self.code,
            "display": display_symbol(self.venue_code, self.code),
        }


MAIN_BOARD = (BOARD_MAIN,)


def is_main_board_universe(code: str) -> bool:
    try:
        return board_for(venue_for_code(code), code) == BOARD_MAIN
    except ValueError:
        return False


def index_uuid(venue: str, code: str) -> uuid.UUID:
    return instrument_uuid(venue, code, "index")
