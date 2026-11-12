"""Deterministic claim extraction rules, claim clustering and claim payloads (spec FINAL §5.1, sources_sectors §5.2).

Why: verification works on CLAIMS, not pages. This module turns a news record into zero or more claim
candidates expressed as ENUMS + EVIDENCE SPANS only (the same shape an LLM provider must return), then
code derives numbers, attribution and the cluster fingerprint from the cited spans.

Invariants: category ids are the taxonomy.json ``event_categories``; a candidate's subject must come from
the record's own vendor stock refs (never guessed from words like "半导体"); assertion types are the
contract enum; polarity is only a hint that feeds the UNCALIBRATED impact prior. Clustering policy
``claim-cluster-v1`` joins a document to an existing claim when subjects agree and either the char-trigram
similarity is >= SIM_THRESHOLD or (same category and identical non-empty numeric facts).
Not covered: coreference, relation extraction beyond keyword rules, multi-claim paragraphs (one headline
claim per document), languages other than Chinese.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from . import origin as origin_mod
from .feeds import NewsRecord, stock_code_from_ref
from .text import normalize, numbers_key, numeric_tokens, similarity, sketch

CLUSTER_POLICY = "claim-cluster-v1"
SIM_THRESHOLD = 0.5
CLUSTER_WINDOW_HOURS = 72
CLAIM_NS = uuid.UUID("c3a1f0d2-77be-4c1f-9a42-5d6e8b0f1a77")
CLAIM_SCHEMA = "news-claim-v1"
EXTRACTION_VERSION = "news-rules-v1"

CATEGORY_IDS = (
    "policy.monetary", "policy.fiscal", "policy.industry", "policy.market_regulation", "macro.release", "macro.rates_fx",
    "global.geopolitics", "global.trade_supply_restriction", "global.cross_asset", "industry.supply_demand", "industry.price_cost",
    "industry.technology_capacity", "company.earnings_guidance", "company.contract_order", "company.capital_action",
    "company.ma_governance", "market.index_passive_flow", "market.microstructure", "event.disruption", "social.attention_sentiment",
)
ASSERTION_TYPES = ("official_disclosure", "independently_reported", "attributed_statement", "opinion", "forecast", "rumor")
ATTRIBUTION_KINDS = ("wire", "broadcaster", "government", "media", "issuer_disclosure", "own_dispatch", "unnamed", "none")
POLARITY = ("+", "-", "mixed", "none")

# first match wins; company.* before macro so a named-company item keeps its company category
_CATEGORY_RULES: list[tuple[str, re.Pattern]] = [(c, re.compile(p)) for c, p in [
    ("company.earnings_guidance", r"业绩预告|业绩快报|业绩说明|年度报告|半年度报告|季度报告|年报|季报|预增|预减|扭亏|首亏|净利润"),
    ("company.capital_action", r"回购|分红|权益分派|利润分配|减持|增持|股权激励|定向增发|可转债|配股|政府补助|质押"),
    ("company.contract_order", r"中标|合同|订单|框架协议|战略合作|供货"),
    ("company.ma_governance", r"收购|重组|并购|诉讼|仲裁|控制权|董事会|股东大会|权益变动|立案|处罚|问询函|监管函"),
    ("policy.monetary", r"降准|降息|MLF|LPR|逆回购|公开市场操作|货币政策"),
    ("policy.fiscal", r"财政部|专项债|国债发行|减税|财政政策|财政赤字"),
    ("policy.market_regulation", r"证监会|交易规则|退市新规|涨跌幅限制|融资融券规则|交易所.{0,6}(?:规则|通知|修订)"),
    ("policy.industry", r"工信部|发改委|产业政策|国务院|行动方案|指导意见|备案"),
    ("macro.release", r"CPI|PPI|GDP|PMI|社融|社会消费品零售|进出口数据|非农|失业率"),
    ("macro.rates_fx", r"美联储|加息|汇率|人民币中间价|美元指数|国债收益率|离岸人民币"),
    ("global.trade_supply_restriction", r"关税|出口管制|禁令|供应限制|制裁|实体清单"),
    ("global.geopolitics", r"俄乌|战争|冲突|袭击|地缘|停火|战俘|导弹|无人机打击"),
    ("global.cross_asset", r"美股|纳指|道指|标普|WTI|布伦特|原油期货|COMEX|黄金期货|现货黄金"),
    ("event.disruption", r"地震|台风|火灾|爆炸|洪水|停电|事故|坠毁"),
    ("market.index_passive_flow", r"指数调整|成分股调整|纳入.{0,6}指数|调出.{0,6}指数|ETF.{0,4}(?:申购|赎回)"),
    ("market.microstructure", r"龙虎榜|涨停|跌停|停牌|复牌|炸板|连板"),
    ("industry.price_cost", r"涨价|提价|降价|价格上调|价格下调|现货价|成本"),
    ("industry.technology_capacity", r"量产|投产|产能|新品发布|突破|研发成功|流片"),
    ("industry.supply_demand", r"库存|产销|销量|出货量|供给|需求|景气"),
    ("social.attention_sentiment", r"热搜|舆情|股吧|人气榜"),
]]
_POS = re.compile(r"回购|增持|中标|预增|扭亏|获批|批准|签订|大幅增长|增长|上调|突破|量产|降准|降息|提价|涨价|纳入|战略合作")
_NEG = re.compile(r"减持|处罚|立案|亏损|预减|首亏|终止|退市|下调|下滑|下降|诉讼|质押|制裁|关税|禁令|冲突|袭击|调出|降价|暂停")
_FORECAST = re.compile(r"预计|预期|将于|(?<![模仿类])拟|有望|或将|可能|计划")
_OPINION = re.compile(r"分析认为|点评|观点|研判|看好|看空|评论称|建议关注")
_RUMOR = re.compile(r"传闻|传言|网传|据传|市场消息|小道消息")
_DENIAL = re.compile(r"辟谣|否认|不实|澄清|子虚乌有|并无此事")

_SECTOR_HINTS = ("半导体", "芯片", "光伏", "新能源", "锂电", "储能", "人工智能", "算力", "机器人", "医药", "创新药", "白酒", "银行", "券商", "地产", "煤炭", "石油", "黄金",
                 "军工", "汽车", "低空经济", "稀土", "面板", "消费电子")


def category_of(text: str, *, has_subject: bool) -> str | None:
    t = normalize_title(text)
    for cat, rx in _CATEGORY_RULES:
        if cat.startswith("company.") and not has_subject:
            continue
        if rx.search(t):
            return cat
    return None


def normalize_title(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def sector_hints(text: str) -> list[str]:
    return [h for h in _SECTOR_HINTS if h in text]


@dataclass(frozen=True)
class Span:
    field: str  # "title" | "body"
    start: int
    end: int


@dataclass(frozen=True)
class Candidate:
    """The enums-and-spans claim shape shared by the rules provider and any LLM provider."""
    category: str | None
    assertion_type: str
    polarity_hint: str
    subject_refs: tuple[str, ...]
    attribution_kind: str
    denial: bool
    spans: tuple[Span, ...]

    def to_structured(self) -> dict:
        return {"category": self.category or "none", "assertion_type": self.assertion_type, "polarity_hint": self.polarity_hint,
                "subject_refs": list(self.subject_refs), "attribution_kind": self.attribution_kind, "denial": self.denial,
                "evidence_spans": [{"field": s.field, "start": s.start, "end": s.end} for s in self.spans]}

    @staticmethod
    def from_structured(d: dict) -> "Candidate":
        return Candidate(None if d["category"] == "none" else d["category"], d["assertion_type"], d["polarity_hint"], tuple(d["subject_refs"]),
                         d["attribution_kind"], bool(d["denial"]), tuple(Span(s["field"], s["start"], s["end"]) for s in d["evidence_spans"]))


def _polarity(text: str) -> str:
    p, n = bool(_POS.search(text)), bool(_NEG.search(text))
    return "mixed" if p and n else "+" if p else "-" if n else "none"


def extract_rules(rec: NewsRecord) -> list[Candidate]:
    """One headline claim per document (rules provider). Subject refs are the record's own A-share refs."""
    text = f"{rec.title}\n{rec.body}"
    refs = tuple(r for r in rec.stock_refs if stock_code_from_ref(r))
    own = "财联社" if rec.own_dispatch else None
    att = origin_mod.detect_attribution(rec.body or rec.title, own_publisher=own)
    kind = "own_dispatch" if rec.own_dispatch else (att.kind if att else "none")
    if rec.own_dispatch and att is not None and att.kind not in ("unnamed",):
        kind = att.kind  # an own dispatch that names an upstream is attributed, not original
    if _RUMOR.search(text):
        atype = "rumor"
    elif _OPINION.search(rec.title) or _OPINION.search(rec.body[:60]):
        atype = "opinion"
    elif _FORECAST.search(rec.title):
        atype = "forecast"
    elif kind in ("wire", "broadcaster", "government", "media", "issuer_disclosure"):
        atype = "attributed_statement"
    else:
        atype = "independently_reported"
    spans = [Span("title", 0, len(rec.title))]
    if att and rec.body:
        spans.append(Span("body", att.start, att.end))
    return [Candidate(category_of(text, has_subject=len(refs) > 0), atype, _polarity(text), refs, kind, bool(_DENIAL.search(text)), tuple(spans))]


def span_text(rec: NewsRecord, span: Span) -> str:
    src = rec.title if span.field == "title" else rec.body
    return src[span.start:span.end]


class CandidateError(ValueError):
    """A provider candidate that violates the code-side rules (subject not in the record, bad span, bad enum)."""


def check_candidate(rec: NewsRecord, c: Candidate) -> None:
    if c.category is not None and c.category not in CATEGORY_IDS:
        raise CandidateError(f"unknown category {c.category!r}")
    if c.assertion_type not in ASSERTION_TYPES or c.attribution_kind not in ATTRIBUTION_KINDS or c.polarity_hint not in POLARITY:
        raise CandidateError("enum value outside contract")
    if any(r not in rec.stock_refs for r in c.subject_refs):
        raise CandidateError("subject_refs must be the record's own vendor refs")
    if c.category and c.category.startswith("company.") and not c.subject_refs:
        raise CandidateError("company category requires a named subject")
    if not c.spans:
        raise CandidateError("a claim needs at least one evidence span")
    for s in c.spans:
        src = rec.title if s.field == "title" else rec.body
        if not (0 <= s.start <= s.end <= len(src)):
            raise CandidateError("evidence span outside the document text")


@dataclass(frozen=True)
class ClaimView:
    """Everything the pipeline derives (in code) from a record + candidate."""
    headline: str
    subject_kind: str
    subject_key: str | None
    predicate: str
    category: str | None
    numbers: list[dict]
    numbers_key: str
    sketch: list[int]
    attribution_publisher: str | None
    assertion_type: str
    polarity_hint: str
    denial: bool
    sector_hints: list[str]
    other_refs: list[str] = field(default_factory=list)


def derive(rec: NewsRecord, c: Candidate) -> ClaimView:
    check_candidate(rec, c)
    codes = [stock_code_from_ref(r) for r in c.subject_refs]
    subject = codes[0] if len(codes) == 1 else None
    kind = "issuer" if subject else ("event" if c.category else "unresolved")
    headline = rec.title.strip()
    cited = " ".join(span_text(rec, s) for s in c.spans)
    nums = numeric_tokens(rec.title + " " + rec.body)
    att = origin_mod.detect_attribution(cited) if any(s.field == "body" for s in c.spans) else None
    predicate = f"{c.category}.reported" if c.category else "news.fact_reported"
    if c.denial:
        predicate = "news.denial_reported"
    return ClaimView(headline, kind, subject, predicate, c.category, nums, numbers_key(nums), sketch(rec.title + " " + rec.body[:120]),
                     att.publisher if att and att.kind != "unnamed" else None, c.assertion_type, c.polarity_hint, c.denial,
                     sector_hints(rec.title + rec.body), [r for r in rec.stock_refs if not stock_code_from_ref(r)])


def same_cluster(a: dict, b: ClaimView) -> bool:
    """``a`` = stored claim payload fingerprint block; policy claim-cluster-v1."""
    if (a.get("subject_key") or None) != (b.subject_key or None):
        return False
    if a.get("predicate") == "news.denial_reported" or b.predicate == "news.denial_reported":
        pass  # a denial joins the cluster of the claim it denies; predicates differ on purpose
    elif a.get("predicate") != b.predicate:
        return False
    if similarity(list(a.get("sketch", [])), b.sketch) >= SIM_THRESHOLD:
        return True
    return bool(b.subject_key and b.category and a.get("category") == b.category and a.get("numbers_key") and a.get("numbers_key") == b.numbers_key)


def claim_uuid(subject_key: str | None, cluster_seed: str) -> uuid.UUID:
    return uuid.uuid5(CLAIM_NS, f"{subject_key or '-'}|{cluster_seed}")


def claim_payload(view: ClaimView, *, cluster_seed: str, verification: dict, impact_event: bool) -> dict:
    return {"headline": view.headline, "category": view.category, "subject_key": view.subject_key, "predicate": view.predicate,
            "numbers": view.numbers, "numbers_key": view.numbers_key, "sketch": view.sketch,
            "cluster": {"policy": CLUSTER_POLICY, "seed": cluster_seed, "similarity_threshold": SIM_THRESHOLD},
            "polarity_hint": view.polarity_hint, "sector_hints": view.sector_hints, "other_refs": view.other_refs,
            "verification": verification, "impact_event": impact_event}


def norm_len(text: str) -> int:
    return len(normalize(text))
