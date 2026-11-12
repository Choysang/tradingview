"""parseScreenText: deterministic Chinese sentence -> typed ScreenAst, with every unmatched fragment flagged.

Why: LEGACY inventory "问句选股 (Ask bar)". The old parser (backend/app/query/nlq.py, read as historical evidence of the
RULE SEMANTICS only) consumed the text rule by rule, masked what each rule matched and reported the residue. This is a new
implementation of that idea against the pulse3 field catalog (``screen-fields-v1``): no model, no memory of stock names,
no SQL; the only output is a whitelisted AST plus the fragments it could not resolve.
Invariants: nothing is dropped silently - residue text is returned as ``ScreenParseIssue`` (reason ``unsupported`` /
``unit_required`` / ``ambiguous``) with spans into the NFKC-lower-cased text; ``complete`` is true only when there is no
issue and at least one condition; a metric the data cannot answer (turnover, seal, PE/PB, themes ...) is ``unsupported``;
a size without its unit (市值大于100) is ``unit_required`` (units are never guessed); contradictory bounds are
``ambiguous``. Rule order matters: specific phrases first, generic metrics last. "或" splits alternatives (any of all).
Not covered: stock names/codes, themes, negation of arbitrary phrases, arithmetic between fields.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import ast as A
from . import catalog as K
from . import patterns as P

MASK = "\x00"
GTE = r"大于等于|高于等于|不低于|不小于|至少|>=|≥"
GT = r"大于|高于|超过|多于|>"
LTE = r"小于等于|低于等于|不超过|不高于|不大于|至多|<=|≤"
LT = r"小于|低于|少于|<"
SEP = r"-|~|到|至|—|–"
CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CNN = r"[一二两三四五六七八九十]{1,3}"
NUM = r"\d+(?:\.\d+)?"
SNUM = r"-?\d+(?:\.\d+)?"
STOP = re.compile("的|股票|个股|筛选|选出|帮我|找出|请|找|查|股|票|要|有|是|在|为|且|并|并且")
SPLITTERS = re.compile(r"[,;、，；。]|并且|而且|以及|同时|且|和|并")


def _cn(text: str) -> float | None:
    if text.isdigit():
        return float(text)
    if "十" in text:
        left, _, right = text.partition("十")
        t = CN.get(left, 0) if left else 1
        o = CN.get(right, 0) if right else 0
        return float(t * 10 + o) if (not left or left in CN) and (not right or right in CN) else None
    return float(CN[text]) if len(text) == 1 and text in CN else None


@dataclass
class Issue:
    start: int
    end: int
    text: str
    reason: str


@dataclass
class _Ctx:
    text: str
    nodes: list[tuple[int, dict]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    sort: list[dict] | None = None
    bounds: dict[str, list[float | None]] = field(default_factory=dict)   # field -> [lo, hi] accumulated across clauses

    def take(self, start: int, end: int, node: dict | None) -> None:
        self.text = self.text[:start] + MASK * (end - start) + self.text[end:]
        if node is not None:
            self.nodes.append((start, node))

    def flag(self, start: int, end: int, reason: str, shown: str) -> None:
        self.text = self.text[:start] + MASK * (end - start) + self.text[end:]
        self.issues.append(Issue(start, end, shown, reason))


def _bounds(text: str, metric: str, unit: str, signed: bool):
    """(start, end, ops, unit_text) with ops = [(operator, number)] or None."""
    num = SNUM if signed else NUM
    tail = (lambda n: rf"\s*(?P<{n}>{unit})?") if unit else (lambda n: "")
    lead = rf"(?:{metric})\s*(?:为|在|介于|是)?\s*"
    m = re.search(rf"{lead}(?P<a>{num}){tail('ua')}\s*(?:{SEP})\s*(?P<b>{num}){tail('ub')}", text)
    if m:
        a, b = float(m["a"]), float(m["b"])
        return m.start(), m.end(), [("gte", min(a, b)), ("lte", max(a, b))], (m["ub"] or m["ua"]) if unit else None
    m = re.search(rf"{lead}(?P<op>{GTE}|{GT}|{LTE}|{LT})\s*(?P<n>{num}){tail('u')}", text)
    if m:
        op = m["op"]
        o = ("gte" if re.fullmatch(GTE, op) else "gt" if re.fullmatch(GT, op) else "lte" if re.fullmatch(LTE, op) else "lt")
        return m.start(), m.end(), [(o, float(m["n"]))], m["u"] if unit else None
    m = re.search(rf"{lead}(?P<n>{num}){tail('u')}\s*(?P<suf>以上|及以上|之上|以下|及以下|之下|以内)", text)
    if m:
        up = m["suf"] in ("以上", "及以上", "之上")
        return m.start(), m.end(), [("gte" if up else "lte", float(m["n"]))], m["u"] if unit else None
    return None


@dataclass(frozen=True)
class Metric:
    field_id: str
    alias: str
    unit: str = ""
    signed: bool = False
    unit_required: bool = False


METRICS: tuple[Metric, ...] = (
    Metric("limit_count_250", r"(?:近一年|近1年|一年内|年内)涨停(?:次数)?", "次"),
    Metric("ret_250d", r"(?:近|最近)?(?:250|一年)日?(?:涨幅|涨跌幅)", "%", True),
    Metric("ret_60d", r"(?:近|最近)?60日(?:涨幅|涨跌幅)", "%", True),
    Metric("ret_20d", r"(?:近|最近)?20日(?:涨幅|涨跌幅)", "%", True),
    Metric("ret_5d", r"(?:近|最近)?5日(?:涨幅|涨跌幅)", "%", True),
    Metric("dist_60d_high", r"距(?:60日)?(?:新高|高点)", "%", True),
    Metric("ma_gap_20", r"(?:20日)?乖离(?:率)?", "%", True),
    Metric("volume_ratio_5", r"量比"),
    Metric("rsi_14", r"rsi(?:14)?"),
    Metric("kdj_j", r"j值|kdj-?j|kdj的?j"),
    Metric("atr_ratio_14", r"atr(?:占比)?", "%"),
    Metric("float_cap", r"流通市值|市值", "亿元|万元|亿|万", unit_required=True),
    Metric("close", r"股价|价格|现价|最新价", "元|块"),
    Metric("pct_chg", r"涨跌幅|涨幅", "%", True),
)
_SORT_KEYS = {"涨幅": "pct_chg", "涨跌幅": "pct_chg", "量比": "volume_ratio_5", "连板数": "limit_streak", "连板": "limit_streak",
              "连阳": "growth_streak", "流通市值": "float_cap", "市值": "float_cap", "股价": "close", "价格": "close"}
_SORT_ALT = "|".join(sorted(_SORT_KEYS, key=len, reverse=True))
_SORT = re.compile(rf"按\s*(?P<k>{_SORT_ALT})\s*(?P<d>从低到高|由低到高|升序|正序|从高到低|由高到低|降序|倒序)?\s*(?:排序|排名|排列)?")


def _emit_bounds(ctx: _Ctx, b, m: Metric) -> bool:
    start, end, ops, unit = b
    shown = ctx.text[start:end].strip()
    if m.unit_required and not unit:
        ctx.flag(start, end, "unit_required", shown)
        return True
    scale = 1.0
    if unit in ("万", "万元"):
        scale = 1 / 10000
    los = [v for o, v in ops if o in ("gt", "gte")]
    his = [v for o, v in ops if o in ("lt", "lte")]
    if los and his and max(los) * scale > min(his) * scale:
        ctx.flag(start, end, "ambiguous", shown)
        return True
    cur = ctx.bounds.setdefault(m.field_id, [None, None])
    lo2 = max([x for x in [cur[0], *(v * scale for v in los)] if x is not None], default=None)
    hi2 = min([x for x in [cur[1], *(v * scale for v in his)] if x is not None], default=None)
    if lo2 is not None and hi2 is not None and lo2 > hi2:
        ctx.flag(start, end, "ambiguous", shown)               # contradicts an earlier clause on the same field
        return True
    cur[0], cur[1] = lo2, hi2
    nodes = [A.cmp(m.field_id, o, round(v * scale, 6)) for o, v in ops]
    ctx.take(start, end, A.all_of(*nodes))
    return True


def _metrics(ctx: _Ctx, metrics: tuple[Metric, ...]) -> None:
    """Numeric metrics, specific aliases first; a mandatory unit that is missing is flagged ``unit_required``."""
    for metric in metrics:
        for _ in range(3):
            b = _bounds(ctx.text, metric.alias, metric.unit, metric.signed)
            if b is None:
                if metric.unit_required:
                    m = re.search(rf"(?:{metric.alias})\s*(?:{GTE}|{GT}|{LTE}|{LT})?\s*{NUM}(?!\s*(?:{metric.unit}))", ctx.text)
                    if m:
                        ctx.flag(m.start(), m.end(), "unit_required", ctx.text[m.start():m.end()])
                        continue
                break
            _emit_bounds(ctx, b, metric)


def _rules(ctx: _Ctx) -> None:
    def search(pattern: str, flags: int = 0):
        return re.search(pattern, ctx.text, flags)

    # sort
    m = _SORT.search(ctx.text)
    if m:
        asc = (m["d"] or "") in ("从低到高", "由低到高", "升序", "正序")
        ctx.sort = [{"field_id": _SORT_KEYS[m["k"]], "direction": "asc" if asc else "desc", "nulls": "last"}]
        ctx.take(m.start(), m.end(), None)
    # pattern names
    for d in P.DEFINITIONS:
        names = {d.name.lower(), d.name.lower().replace(" ", "")}
        for nm in names:
            while (i := ctx.text.find(nm)) >= 0:
                ctx.take(i, i + len(nm), A.cmp(f"pattern.{d.id}", "eq", 1))
    _metrics(ctx, METRICS[:1])      # 近一年涨停 must be read before the bare 涨停 keyword
    # limit board / streak phrases
    for _ in range(4):
        m = re.search(rf"(?P<n>\d+|{CNN})\s*(?:连板|板)\s*(?P<q>以上|及以上|以下|及以下|以内)?", ctx.text)
        if not m:
            break
        n = _cn(m["n"])
        if n is None:
            ctx.flag(m.start(), m.end(), "ambiguous", ctx.text[m.start():m.end()])
            continue
        q = m["q"]
        op = "gte" if q in ("以上", "及以上") else "lte" if q else "eq"
        ctx.take(m.start(), m.end(), A.cmp("limit_streak", op, int(n)))
    for pat, node in ((r"首板", A.cmp("limit_streak", "eq", 1)), (r"高标", A.cmp("limit_streak", "gte", 5)),
                      (r"连续涨停|连板", A.cmp("limit_streak", "gte", 2)), (r"涨停(?:板|股|池)?", A.cmp("limit_streak", "gte", 1))):
        m = search(pat)
        if m:
            ctx.take(m.start(), m.end(), node)
    for _ in range(3):
        m = re.search(rf"(?P<n>\d+|{CNN})\s*连[阳涨]\s*(?:天|日)?\s*(?:以上|及以上)?", ctx.text)
        if not m:
            break
        n = _cn(m["n"])
        if n is None:
            ctx.flag(m.start(), m.end(), "ambiguous", ctx.text[m.start():m.end()])
            continue
        ctx.take(m.start(), m.end(), A.cmp("growth_streak", "gte", int(n)))
    m = search(r"连阳|连续上涨")
    if m:
        ctx.take(m.start(), m.end(), A.cmp("growth_streak", "gte", 3))
    _metrics(ctx, METRICS[1:])
    # keywords
    for pat, node in ((r"(?:接近|逼近|临近)(?:60日)?新高", A.cmp("dist_60d_high", "gte", -5)),
                      (r"创?(?:60日|六十日)?新高|突破(?:60日)?新高", A.cmp("dist_60d_high", "gte", 0)),
                      (r"放量", A.cmp("volume_ratio_5", "gte", 2)), (r"缩量", A.cmp("volume_ratio_5", "lte", 0.7)),
                      (r"上涨", A.cmp("pct_chg", "gt", 0)), (r"下跌", A.cmp("pct_chg", "lt", 0))):
        m = search(pat)
        if m:
            ctx.take(m.start(), m.end(), node)
    m = search(r"(?:不含|剔除|排除|非|去掉|不要|不带|不包含)\s*\*?st")
    if m:
        ctx.take(m.start(), m.end(), A.cmp("is_st", "eq", 0))
    m = search(r"(?:不含|剔除|排除|不要|去掉)次新(?:股)?")
    if m:
        ctx.take(m.start(), m.end(), A.cmp("is_new", "eq", 0))
    boards = []
    for pat, fid in ((r"沪深主板|主板", "board_main"), (r"创业板", "board_chinext"), (r"科创板", "board_star"), (r"北交所|北交", "board_bse")):
        m = search(pat)
        if m:
            boards.append((m.start(), A.cmp(fid, "eq", 1)))
            ctx.take(m.start(), m.end(), None)
    if boards:
        ctx.nodes.append((boards[0][0], A.any_of(*[n for _, n in sorted(boards, key=lambda b: b[0])])))


def _residue(ctx: _Ctx, original: str) -> None:
    for m in re.finditer(rf"[^\s{MASK}]+", ctx.text):
        chunk = m.group()
        bare = STOP.sub("", chunk)
        if not bare:
            continue
        ctx.issues.append(Issue(m.start(), m.end(), original[m.start():m.end()], "unsupported"))


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def parse(text: str, field_catalog_version: str = K.CATALOG_VERSION) -> dict:
    """{"ast": dict|None, "unresolved": [issue wire], "complete": bool}; raises ValueError for an unknown catalog version."""
    if field_catalog_version != K.CATALOG_VERSION:
        raise ValueError(f"unknown field catalog version {field_catalog_version!r}")
    norm = normalize(text)
    blank = " " * len(norm)
    alternatives = re.split(r"或者|或", norm)
    pos = 0
    groups: list[dict] = []
    issues: list[Issue] = []
    sort: list[dict] | None = None
    for alt in alternatives:
        start = norm.index(alt, pos) if alt else pos
        pos = start + len(alt)
        masked = (blank[:start] + SPLITTERS.sub(lambda m: " " * len(m.group()), alt) + blank[pos:])
        ctx = _Ctx(masked)
        _rules(ctx)
        _residue(ctx, norm)
        issues.extend(ctx.issues)
        sort = ctx.sort or sort
        nodes = [n for _, n in sorted(ctx.nodes, key=lambda x: x[0])]
        if nodes:
            groups.append(A.all_of(*nodes))
    issues.sort(key=lambda i: i.start)
    cond = A.any_of(*groups) if groups else None
    ast = A.make_ast(cond, sort) if cond else None
    if ast:
        A.validate_ast(ast)
    wire = [{"source_start": i.start, "source_end": i.end, "text": i.text, "reason_code": i.reason} for i in issues]
    return {"ast": ast, "unresolved": wire, "complete": bool(ast) and not wire}
