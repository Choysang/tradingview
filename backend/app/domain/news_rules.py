"""News message -> event class: hand-written keyword rules for the alert process (E075).

Two scopes. Company-level rules say something about the company a message names (a probe, an earnings warning, a
contract win); the stocks come from the message itself. Macro and policy rules (rate cuts, tariffs, oil, pork...)
carry 申万 level-2 industries in the style of event_links.py; the main-board members of those industries are used
only to check against the holdings and the watch list, never as alerts on their own.

A rule fires when one of `any_of` matches, every `must` pattern matches too and no `unless` pattern matches
(`unless` holds the "exclusion" words: a withdrawn probe is not a probe). Texts are matched as title + body after
NFKC normalisation. Every hit carries its rule id, so an alert records which rule fired.

The rules are v1 judgements (2026-11-08), candidate parameters like the ones in event_links.py and board_rules.py:
a change is a new RULES_VERSION and a new ledger entry.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

RULES_VERSION = "news-rules-v1"
NEGATIVE, POSITIVE = "negative", "positive"


@dataclass(frozen=True)
class Rule:
    id: str
    cls: str
    direction: str  # negative | positive
    magnitude: int  # 1..3
    any_of: tuple[str, ...]
    must: tuple[str, ...] = ()
    unless: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()  # 申万 level-2; non-empty means a macro rule

    def matches(self, text: str) -> bool:
        if not _rx(self.any_of).search(text):
            return False
        if any(not _rx((m,)).search(text) for m in self.must):
            return False
        return not (self.unless and _rx(self.unless).search(text))


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    cls: str
    direction: str
    magnitude: int
    industries: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    hits: tuple[RuleHit, ...]

    @property
    def company(self) -> tuple[RuleHit, ...]:
        return tuple(h for h in self.hits if not h.industries)

    @property
    def macro(self) -> tuple[RuleHit, ...]:
        return tuple(h for h in self.hits if h.industries)

    def company_verdict(self) -> tuple[str, int] | None:
        """(direction, magnitude) of the company-level hits; a negative hit wins over a positive one."""
        return _verdict(self.company)


@lru_cache(maxsize=None)
def _rx(patterns: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns))


def _verdict(hits: tuple[RuleHit, ...]) -> tuple[str, int] | None:
    for direction in (NEGATIVE, POSITIVE):
        mags = [h.magnitude for h in hits if h.direction == direction]
        if mags:
            return direction, max(mags)
    return None


def normalize(text: str) -> str:
    """NFKC (full-width letters, digits and punctuation become ASCII) so patterns and names meet one spelling."""
    return unicodedata.normalize("NFKC", text or "")


# --- company-level rules --------------------------------------------------------------------------

COMPANY_RULES: tuple[Rule, ...] = (
    Rule("N-REG-01", "regulatory", NEGATIVE, 3, ("立案调查", "立案告知", "行政处罚"), unless=("撤销", "终止调查")),
    Rule("N-EARN-01", "earnings_warning", NEGATIVE, 2, ("预亏", r"预减(?!持)", "首亏", "续亏")),
    Rule("N-DELIST-01", "delisting_risk", NEGATIVE, 3, ("退市风险", "风险警示"), unless=("撤销", "申请撤销", "解除")),
    Rule("N-SELL-01", "share_selldown", NEGATIVE, 1, ("减持计划", "预披露", "拟减持"), must=("减持",),
         unless=("不减持", "终止", "完成", "结果", "届满", "实施完毕")),
    Rule("N-INQ-01", "inquiry_letter", NEGATIVE, 2, ("问询函",)),
    Rule("N-LOCK-01", "lockup_expiry", NEGATIVE, 1, ("首次公开发行", "限售股", "非公开发行"), must=("解除限售",),
         unless=("股权激励", "期权", "限制性股票", "员工持股")),
    Rule("N-HALT-01", "production_halt", NEGATIVE, 2, ("停产",)),
    Rule("N-ACCIDENT-01", "accident", NEGATIVE, 2, ("爆炸", "事故")),
    Rule("N-FREEZE-01", "assets_frozen", NEGATIVE, 2, ("被冻结",)),
    Rule("N-GUAR-01", "illegal_guarantee", NEGATIVE, 3, ("违规担保",)),
    Rule("N-OCCUPY-01", "fund_occupation", NEGATIVE, 3, ("资金占用",),
         unless=("不存在", "不涉及", "已归还", "已清偿", "已解决", "解除")),
    Rule("N-DOWNGRADE-01", "rating_downgrade", NEGATIVE, 1, (r"下调.{0,4}评级", r"评级.{0,3}下调")),
    Rule("P-WIN-01", "contract_win", POSITIVE, 2, ("中标",), unless=("未中标", "落标", "未能中标")),
    Rule("P-CONTRACT-01", "major_contract", POSITIVE, 2, (r"签[订署].{0,30}重大合同",)),
    Rule("P-BUYBACK-01", "buyback", POSITIVE, 1, (r"(?<!逆)回购",), unless=("终止回购", "取消回购", "回购.{0,6}终止")),
    Rule("P-INCREASE-01", "shareholder_increase", POSITIVE, 1, ("增持",), unless=("减持", "不增持", "终止增持")),
    Rule("P-EARN-01", "earnings_upside", POSITIVE, 2, ("业绩预增", "扭亏"), unless=("无法扭亏", "未能扭亏", "难以扭亏")),
    Rule("P-MA-01", "restructuring", POSITIVE, 2, ("重组", "收购"), unless=("终止", "取消", "失败")),
    Rule("P-APPROVAL-01", "approval", POSITIVE, 1, ("获批", r"获得.{0,20}注册证"), unless=("未获批", "不予")),
)

# --- macro and policy rules -> 申万 level-2 industries ----------------------------------------------

_PROPERTY = ("房地产开发", "房地产服务", "房屋建设Ⅱ", "装修建材", "装修装饰Ⅱ")
_RATE_INDUSTRIES = ("房地产开发", "房地产服务", "证券Ⅱ", "房屋建设Ⅱ", "基础建设")
_FOREIGN_BANKS = ("美联储", "欧洲央行", "日本央行", "英国央行", "加息")
_OIL = r"(?:油价|原油|布伦特|WTI)"
_PORK = r"(?:猪价|生猪|猪肉)"
_TARIFF_INDUSTRIES = ("贸易Ⅱ", "航运港口", "纺织制造", "服装家纺", "家居用品", "小家电", "消费电子")
_NEV_INDUSTRIES = ("乘用车", "汽车零部件", "电池", "能源金属")

MACRO_RULES: tuple[Rule, ...] = (
    Rule("M-RATE-CUT-01", "rate_cut", POSITIVE, 2,
         ("降息", r"下调.{0,12}(?:利率|LPR)", r"降低.{0,10}利率", r"(?:LPR|利率).{0,8}(?:下调|下降|下行)"),
         unless=_FOREIGN_BANKS + ("不降息", "暂停降息"), industries=_RATE_INDUSTRIES),
    Rule("M-RRR-CUT-01", "reserve_ratio_cut", POSITIVE, 2, ("降准", r"(?:下调|降低).{0,6}存款准备金率"),
         unless=_FOREIGN_BANKS + ("不降准",), industries=_RATE_INDUSTRIES),
    Rule("M-FED-CUT-01", "fed_cut", POSITIVE, 1, (r"美联储.{0,15}降息", r"降息.{0,10}美联储"),
         unless=("不降息", "暂停降息", "不会降息", "放缓降息"), industries=("贵金属", "工业金属", "小金属")),
    Rule("M-RE-EASE-01", "property_easing", POSITIVE, 2,
         (r"(?:放松|取消|优化|降低).{0,10}(?:限购|首付|房贷|购房)", r"公积金.{0,6}(?:下调|降低|放宽)", r"收储.{0,6}商品房",
          r"房地产.{0,6}(?:利好|新政)"), unless=("收紧",), industries=_PROPERTY),
    Rule("M-RE-TIGHT-01", "property_tightening", NEGATIVE, 2, (r"(?:收紧|加强).{0,6}(?:限购|调控)", r"房企.{0,8}(?:违约|暴雷)"),
         industries=_PROPERTY),
    Rule("M-CHIP-CTRL-01", "chip_export_control", NEGATIVE, 3,
         (r"(?:出口管制|实体清单|制裁).{0,30}(?:芯片|半导体|光刻)", r"(?:芯片|半导体|光刻).{0,30}(?:出口管制|实体清单|制裁|禁令)"),
         unless=("取消", "解除", "放宽"), industries=("半导体", "元件", "计算机设备", "通信设备")),
    Rule("M-CHIP-SUPPORT-01", "chip_support", POSITIVE, 2,
         (r"(?:芯片|半导体|集成电路).{0,12}(?:扶持|补贴|大基金|国产替代|专项)",), industries=("半导体", "电子化学品Ⅱ", "元件")),
    Rule("M-TARIFF-UP-01", "tariff_up", NEGATIVE, 2, (r"加征.{0,6}关税", r"提高.{0,6}关税", r"关税.{0,6}(?:上调|提高|加征)"),
         unless=("暂停", "取消", "豁免", "下调", "降低", "休战"), industries=_TARIFF_INDUSTRIES),
    Rule("M-TARIFF-DOWN-01", "tariff_down", POSITIVE, 2,
         (r"(?:取消|降低|暂停|豁免).{0,6}关税", r"关税.{0,6}(?:下调|豁免|暂停)", "关税休战"), industries=_TARIFF_INDUSTRIES),
    Rule("M-OIL-UP-POS-01", "oil_up", POSITIVE, 2,
         (_OIL + r".{0,12}(?:大涨|上涨|飙升|涨超|攀升|走高|突破)",), industries=("油气开采Ⅱ", "油服工程", "煤炭开采")),
    Rule("M-OIL-UP-NEG-01", "oil_up", NEGATIVE, 2,
         (_OIL + r".{0,12}(?:大涨|上涨|飙升|涨超|攀升|走高|突破)",), industries=("航空机场", "物流")),
    Rule("M-OIL-DOWN-NEG-01", "oil_down", NEGATIVE, 2,
         (_OIL + r".{0,12}(?:大跌|下跌|暴跌|跌超|走低|回落|重挫|跌破)",), industries=("油气开采Ⅱ", "油服工程")),
    Rule("M-OIL-DOWN-POS-01", "oil_down", POSITIVE, 2,
         (_OIL + r".{0,12}(?:大跌|下跌|暴跌|跌超|走低|回落|重挫|跌破)",), industries=("航空机场", "物流")),
    Rule("M-PORK-UP-01", "pork_up", POSITIVE, 2, (_PORK + r".{0,12}(?:上涨|涨价|大涨|反弹|攀升|走高|上行|涨超)",),
         industries=("养殖业",)),
    Rule("M-PORK-DOWN-01", "pork_down", NEGATIVE, 2, (_PORK + r".{0,12}(?:下跌|下降|走低|新低|回落|大跌|跌破|低迷)",),
         industries=("养殖业",)),
    Rule("M-NEV-SUBSIDY-01", "nev_subsidy", POSITIVE, 2,
         (r"新能源.{0,12}(?:补贴|购置税|免征|减免|下乡|以旧换新)", "汽车以旧换新", r"充电桩.{0,8}(?:补贴|政策|支持)"),
         unless=("退坡", "取消", "退出", "恢复征收", "不再"), industries=_NEV_INDUSTRIES),
    Rule("M-NEV-SUBSIDY-CUT-01", "nev_subsidy_cut", NEGATIVE, 2,
         (r"新能源.{0,12}(?:补贴|购置税).{0,8}(?:退坡|取消|退出|恢复征收|减半)", r"(?:退坡|取消|退出).{0,8}新能源.{0,8}补贴"),
         industries=_NEV_INDUSTRIES),
    Rule("M-SEC-POLICY-01", "securities_policy", POSITIVE, 2,
         (r"印花税.{0,8}(?:下调|减半|降低|减免)", r"(?:下调|降低|减半).{0,6}印花税", "活跃资本市场"), industries=("证券Ⅱ", "多元金融")),
    Rule("M-VBP-01", "volume_based_procurement", NEGATIVE, 2,
         (r"国家(?:组织)?(?:药品|高值医用耗材|医用耗材)?(?:集中)?带量采购", r"第.{1,3}批.{0,6}集采", r"集采.{0,8}(?:降价|平均降幅|最高降幅)"),
         unless=("暂停", "取消", "不再"), industries=("化学制药", "医疗器械")),
)

RULES: tuple[Rule, ...] = COMPANY_RULES + MACRO_RULES

# Words that make an unclassified message look like macro or policy news (the optional LLM fallback only).
MACRO_HINTS = ("央行", "国务院", "证监会", "发改委", "财政部", "工信部", "商务部", "住建部", "银保监", "金融监管", "政策", "关税",
               "降息", "降准", "加息", "美联储", "补贴", "出口管制", "制裁", "OPEC", "油价", "猪价", "意见稿", "规划", "新规", "国常会")


def classify(title: str, body: str = "") -> Classification:
    """Every rule that fires on the message, company-level and macro, in rule order."""
    text = normalize(f"{title}\n{body}")
    return Classification(tuple(RuleHit(r.id, r.cls, r.direction, r.magnitude, r.industries) for r in RULES if r.matches(text)))


def looks_macro(title: str, body: str = "") -> bool:
    text = normalize(f"{title}\n{body}")
    return any(h in text for h in MACRO_HINTS)
