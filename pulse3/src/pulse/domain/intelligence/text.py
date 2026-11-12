"""Text normalisation, bottom-k shingle sketches and numeric-token extraction (spec sources_sectors §5.2).

Why: claim clustering needs a deterministic, explainable near-duplicate test between short news
texts, and the "same number" test needs amounts parsed with their unit.

Invariants: everything is deterministic (sha1 based, no randomness, no model); similarity is the
bottom-k MinHash estimate of char-trigram Jaccard; thresholds live in ``claims.CLUSTER_POLICY`` and
are engineering parameters, not calibrated truth. Amounts are ``Decimal``, never float.
Not covered: semantic similarity, entity linking, translation.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from decimal import Decimal

SKETCH_K = 48
_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"[\s　，。、；：！？“”‘’（）()【】\[\]《》<>\"'`~·…—\-_,.;:!?/\\|]+")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = _TAG.sub("", t)
    return _DROP.sub("", t).lower()


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.sha1(s.encode("utf-8")).digest()[:8], "big")


def sketch(text: str, k: int = SKETCH_K, n: int = 3) -> list[int]:
    """Bottom-k hashes of the char n-gram set (sorted ascending, unique)."""
    t = normalize(text)
    if len(t) < n:
        grams = {t} if t else set()
    else:
        grams = {t[i:i + n] for i in range(len(t) - n + 1)}
    return sorted({_h64(g) for g in grams})[:k]


def similarity(a: list[int], b: list[int], k: int = SKETCH_K) -> float:
    """Bottom-k Jaccard estimate; 0.0 when either sketch is empty."""
    if not a or not b:
        return 0.0
    union = sorted(set(a) | set(b))[:k]
    sa, sb = set(a), set(b)
    both = sum(1 for h in union if h in sa and h in sb)
    return both / len(union)


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]


_NUM = re.compile(r"(?P<n>\d+(?:,\d{3})*(?:\.\d+)?)\s*(?P<u>万亿|亿|万|千|%|％|倍|个百分点)?\s*(?P<c>元|美元|港元|股|手|吨|亿元)?")
_UNIT_MUL = {"万亿": Decimal("1000000000000"), "亿": Decimal("100000000"), "万": Decimal("10000"), "千": Decimal("1000"), None: Decimal(1)}


def numeric_tokens(text: str) -> list[dict]:
    """Amounts / percentages with their stated unit, e.g. ``1090万元`` -> value 10900000 unit CNY.

    Only tokens that carry a unit (万/亿/%/currency/倍) are kept: bare integers (dates, codes) are not facts.
    """
    out: list[dict] = []
    for m in _NUM.finditer(unicodedata.normalize("NFKC", text or "")):
        u, c = m.group("u"), m.group("c")
        if u is None and c is None:
            continue
        num = Decimal(m.group("n").replace(",", ""))
        if u in ("%", "％"):
            out.append({"raw": m.group(0).strip(), "value": str(num), "unit": "percent"})
        elif u in ("倍", "个百分点"):
            out.append({"raw": m.group(0).strip(), "value": str(num), "unit": u})
        else:
            unit = {"元": "CNY", "美元": "USD", "港元": "HKD", "亿元": "CNY", "股": "shares", "手": "lots", "吨": "tonnes"}.get(c or "", "CNY" if u else "number")
            mul = _UNIT_MUL[u]
            out.append({"raw": m.group(0).strip(), "value": str(num * mul), "unit": unit})
    return out


def numbers_key(tokens: list[dict]) -> str:
    return "|".join(sorted({f"{t['value']}:{t['unit']}" for t in tokens}))
