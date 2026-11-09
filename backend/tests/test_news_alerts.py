"""E075 news alerts: rules, mapping, levels, dedup across a restart, the optional LLM path (mocked, no network)."""
from __future__ import annotations

import gzip
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import polars as pl
import pytest

from backend.app.domain import news_rules as R
from backend.app.domain.event_links import load_members
from backend.app.warehouse import news as N
from backend.app.warehouse import news_alerts as A

FIX = Path(__file__).parent / "fixtures" / "news_alerts"
DAY = date(2026, 11, 6)
SECRET = "sk-TESTSECRET-0123456789"
UTC = timezone.utc


# --- helpers --------------------------------------------------------------------------------------

def answers(name: str = "answers.json") -> list[dict]:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def write_raw(day_dir: Path, items: list[dict]) -> None:
    """One gzip member per answer, appended as news.py's recorder does."""
    (day_dir / "raw").mkdir(parents=True, exist_ok=True)
    for a in items:
        with gzip.open(day_dir / "raw" / f"{a['source']}.jsonl.gz", "at", encoding="utf-8") as fh:
            fh.write(json.dumps({"received_at": a["received_at"], "items": a["items"]}, ensure_ascii=False, default=str) + "\n")


def names_index() -> A.NameIndex:
    return A.NameIndex.from_frame(pl.read_csv(FIX / "securities.csv", schema_overrides={"code": pl.Utf8}))


def members_for(tmp: Path) -> dict[str, frozenset[str]]:
    sw = pl.read_csv(FIX / "sw_industry.csv", schema_overrides={"code": pl.Utf8, "joined": pl.Utf8})
    path = tmp / "sw_industry.parquet"
    sw.write_parquet(path)
    return load_members(path, DAY)


@pytest.fixture
def day_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PULSE_NEWS_DIR", str(tmp_path / "news"))
    monkeypatch.setenv("PULSE_HOLDINGS_FILE", str(FIX / "book.json"))
    monkeypatch.setenv("PULSE_SW_INDUSTRY", str(tmp_path / "sw_industry.parquet"))
    monkeypatch.setenv("PULSE_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PULSE_ALERT_MODEL", raising=False)
    monkeypatch.delenv("PULSE_ALERT_LLM_CAP", raising=False)
    members_for(tmp_path)
    path = tmp_path / "news" / DAY.isoformat()
    path.mkdir(parents=True)
    return path


def engine(day_dir: Path, tmp_path: Path, *, book: Path | None = FIX / "book.json", key: str | None = None,
           client: httpx.Client | None = None, sleeps: list[float] | None = None) -> A.AlertEngine:
    return A.AlertEngine(day_dir, DAY, book_path=book, names=names_index(), members=members_for(tmp_path),
                         key_loader=lambda: key, llm_client=client, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def rows_of(day_dir: Path) -> list[dict]:
    return A.read_alert_rows(day_dir)


def by(rows: list[dict], source: str, item_id: str) -> dict[str, dict]:
    return {r["level"]: r for r in rows if r["source"] == source and r["item_id"] == item_id}


# --- rules ----------------------------------------------------------------------------------------

RULE_TABLE = [
    ("公司收到证监会立案告知书", {"N-REG-01"}),
    ("公司收到行政处罚决定书", {"N-REG-01"}),
    ("证监会决定撤销立案调查", set()),  # unless 撤销
    ("公司收到终止调查通知书，此前被立案调查", set()),  # unless 终止调查
    ("预计2026年度业绩预亏", {"N-EARN-01"}),
    ("公司首亏", {"N-EARN-01"}),
    ("公司续亏", {"N-EARN-01"}),
    ("三季度净利润预减", {"N-EARN-01"}),
    ("股东预减持股份", {"N-SELL-01"}),  # 预减持 is a selldown, not an earnings warning
    ("公司股票被实施退市风险警示", {"N-DELIST-01"}),
    ("公司股票被实施风险警示", {"N-DELIST-01"}),
    ("公司申请撤销退市风险警示", set()),  # unless 撤销 / 申请撤销
    ("公司股票将被解除风险警示", set()),  # unless 解除
    ("股东拟减持不超过3%股份", {"N-SELL-01"}),
    ("股东发布减持计划", {"N-SELL-01"}),
    ("股东减持股份预披露公告", {"N-SELL-01"}),
    ("股东承诺不减持，另发布减持计划", {"N-SELL-01"}),  # 不减持 only clears its own clause
    ("股东终止减持计划", set()),  # unless 终止
    ("股东减持计划已完成", set()),  # unless 完成
    ("股东减持计划实施结果", set()),  # unless 结果
    ("股东减持计划期限届满", set()),  # unless 届满
    ("股东减持计划实施完毕", set()),  # unless 实施完毕
    ("交易所向公司发出问询函", {"N-INQ-01"}),
    ("首次公开发行限售股解除限售上市流通", {"N-LOCK-01"}),
    ("非公开发行股份解除限售", {"N-LOCK-01"}),
    ("限售股解除限售并上市流通", {"N-LOCK-01"}),
    ("首次公开发行限售股及股权激励解除限售", set()),  # unless 股权激励
    ("限售股与期权一并解除限售", set()),  # unless 期权
    ("限售股与限制性股票一并解除限售", set()),  # unless 限制性股票
    ("非公开发行及员工持股计划解除限售", set()),  # unless 员工持股
    ("解除限售的限制性股票上市", set()),  # no 首次公开发行 / 限售股 / 非公开发行
    ("子公司停产检修", {"N-HALT-01"}),
    ("化工厂发生爆炸", {"N-ACCIDENT-01"}),
    ("矿山发生安全事故", {"N-ACCIDENT-01"}),
    ("控股股东所持股份被冻结", {"N-FREEZE-01"}),
    ("公司存在违规担保", {"N-GUAR-01"}),
    ("控股股东资金占用", {"N-OCCUPY-01"}),
    ("公司不存在资金占用情形", set()),
    ("资金占用已归还", set()),
    ("券商下调评级至中性", {"N-DOWNGRADE-01"}),
    ("公司中标某地铁项目", {"P-WIN-01"}),
    ("公司未中标", set()),
    ("公司落标", set()),
    ("公司签订5亿元重大合同", {"P-CONTRACT-01"}),
    ("公司签署日常经营重大合同", {"P-CONTRACT-01"}),
    ("公司拟回购股份", {"P-BUYBACK-01"}),
    ("央行开展逆回购操作", set()),  # a reverse repo is not a buyback
    ("公司终止回购", set()),
    ("公司取消回购方案", set()),
    ("公司回购计划提前终止", set()),
    ("股东增持公司股份", {"P-INCREASE-01"}),
    ("股东增持后又减持", set()),  # but not 减持
    ("股东承诺不增持", set()),
    ("公司业绩预增", {"P-EARN-01"}),
    ("公司扭亏为盈", {"P-EARN-01"}),
    ("公司无法扭亏", set()),
    ("公司拟收购某资产", {"P-MA-01"}),
    ("公司筹划资产重组", {"P-MA-01"}),
    ("公司终止收购", {"N-TERM-01"}),  # not P-MA-01, and since v2 a terminated deal is its own bad news
    ("公司取消重组", set()),
    ("公司重组失败", {"N-TERM-01"}),
    ("产品获批上市", {"P-APPROVAL-01"}),
    ("公司获得医疗器械注册证", {"P-APPROVAL-01"}),
    ("申请未获批", set()),
    ("央行宣布降息", {"M-RATE-CUT-01"}),
    ("LPR下调10个基点", {"M-RATE-CUT-01"}),
    ("美联储宣布降息25个基点", {"M-FED-CUT-01"}),  # not a domestic rate cut
    ("美联储暂停降息", set()),
    ("央行宣布降准0.5个百分点", {"M-RRR-CUT-01"}),
    ("下调存款准备金率", {"M-RRR-CUT-01"}),
    ("多地放松限购政策", {"M-RE-EASE-01"}),
    ("住房公积金贷款利率下调", {"M-RE-EASE-01", "M-RATE-CUT-01"}),
    ("多地收紧限购", {"M-RE-TIGHT-01"}),
    ("美国扩大对华芯片出口管制", {"M-CHIP-CTRL-01"}),
    ("美方取消对华半导体出口管制", set()),
    ("大基金加大对半导体国产替代的扶持", {"M-CHIP-SUPPORT-01"}),
    ("美国对华加征关税", {"M-TARIFF-UP-01"}),
    ("美国暂停加征关税", {"M-TARIFF-DOWN-01"}),
    ("中美关税休战", {"M-TARIFF-DOWN-01"}),
    ("国际油价大涨", {"M-OIL-UP-POS-01", "M-OIL-UP-NEG-01"}),
    ("布伦特原油大跌", {"M-OIL-DOWN-NEG-01", "M-OIL-DOWN-POS-01"}),
    ("生猪价格上涨", {"M-PORK-UP-01"}),
    ("猪价继续下跌", {"M-PORK-DOWN-01"}),
    ("新能源汽车购置税减免延续", {"M-NEV-SUBSIDY-01"}),
    ("汽车以旧换新补贴加码", {"M-NEV-SUBSIDY-01"}),
    ("新能源汽车购置税退坡", {"M-NEV-SUBSIDY-CUT-01"}),
    ("印花税下调", {"M-SEC-POLICY-01"}),
    ("国家组织药品集中带量采购启动", {"M-VBP-01"}),
    ("第十一批集采暂停", set()),
    ("今日天气晴朗", set()),
]


@pytest.mark.parametrize("text,expected", RULE_TABLE, ids=[t for t, _ in RULE_TABLE])
def test_rule_table(text: str, expected: set[str]):
    assert {h.rule_id for h in R.classify(text).hits} == expected


def test_rule_ids_are_unique_and_directions_valid():
    ids = [r.id for r in R.RULES]
    assert len(ids) == len(set(ids)) and R.RULES_VERSION
    assert all(r.direction in (R.NEGATIVE, R.POSITIVE) and 1 <= r.magnitude <= 3 for r in R.RULES)
    assert all(r.industries for r in R.MACRO_RULES) and not any(r.industries for r in R.COMPANY_RULES)


def test_macro_industries_exist_in_the_sw_table():
    path = A.DEFAULT_SW_FILE
    if not path.exists():
        pytest.skip("no local sw_industry.parquet")
    known = set(pl.read_parquet(path, columns=["sw2"])["sw2"].to_list())
    assert {i for r in R.MACRO_RULES for i in r.industries} <= known


def test_company_verdict_negative_wins_and_body_counts_only_without_a_title():
    assert R.classify("公司签订重大合同并收到问询函").company_verdict() == (R.NEGATIVE, 2)
    assert R.classify("公司签订重大合同", "同时收到问询函").company_verdict() == (R.POSITIVE, 2)
    assert R.classify("", "公司中标项目").company_verdict() == (R.POSITIVE, 2)
    assert R.classify("", "财联社11月9日电，公司收到问询函").company_verdict() == (R.NEGATIVE, 2)
    assert R.classify("今日天气晴朗").company_verdict() is None
    assert R.looks_macro("国务院发布新规") and not R.looks_macro("今日天气晴朗")


# Patterns of the wrong verdicts in the recorded messages of 2026-11-02 ~ 11-09 (E088), company names replaced.
V2_CASES = [
    # (title, body, direction or None)
    ("甲公司：董事长提议回购4亿至5亿元股份", "未来三个月无减持计划", R.POSITIVE),  # was read as a selldown
    ("甲公司：公司及相关责任人被出具警示函", "公司此前披露收购股权暨签署协议", R.NEGATIVE),  # was read as a takeover
    ("甲公司：公司及相关责任人收到证监局行政监管措施决定书", "", R.NEGATIVE),
    ("甲公司：股东乙减持300万股，合计持股5.68%", "公司此前披露收购事项", R.NEGATIVE),
    ("甲公司：因提供虚假资料 子公司被暂停中标资格2年", "", R.NEGATIVE),
    ("甲公司：机器人业务现阶段收入和利润占比极低", "公司控股股东增持", R.NEGATIVE),
    ("甲公司：相关业务商业化能力具有一定不确定性", "", R.NEGATIVE),
    ("甲公司：公司不涉及生产半导体设备", "", R.NEGATIVE),
    ("甲公司：股价异动，澄清不从事机器人业务", "", R.NEGATIVE),  # a denied concept stays bad for a stock that ran on it
    ("甲公司：副总经理乙被实施留置", "", R.NEGATIVE),
    ("甲公司：财务总监乙因个人原因辞职", "", R.NEGATIVE),
    ("甲公司：副总经理乙因工作调动辞职", "", None),
    ("甲公司：合同对方再次违约 公司已发催告函", "", R.NEGATIVE),
    ("甲公司：终止14.9亿元项目投资框架协议", "", R.NEGATIVE),
    ("甲公司：网传产品停产等消息不实", "", None),  # a denied rumour is not the event
    ("甲公司澄清：物料短缺及生产停工传闻不实", "", None),
    ("甲公司：未因涉嫌内幕交易被立案", "", None),
    ("甲公司：绝大部分客户未下修需求", "分析师下调评级的说法不准确", None),
    ("甲公司：拟注册发行30亿元公司债券", "此前收购事项已完成", None),
    ("甲公司：将维护公司价值和投资者利益", "公司将结合回购再贷款政策", None),
    ("商务部：对进口某化工品进行反倾销立案调查", "", None),
    ("乙公司研究员：自动驾驶的最大意义是减少事故", "", None),
    ("甲公司：子公司发生火情事故 未造成人员伤亡", "", R.NEGATIVE),
    ("某机构将某科技股评级上调至增持", "", None),
    # review of v2: a real event with a denial in another clause stays bad; a denial in its own clause does not count
    ("甲公司：控股股东被立案调查 公司称与经营无关", "", R.NEGATIVE),
    ("甲公司：控股股东被立案，与公司无关，公司拟回购", "", R.NEGATIVE),
    ("甲公司：收到证监会立案告知书，否认相关传闻", "", R.NEGATIVE),
    ("甲公司：关于收到文件的公告", "公司收到《立案告知书》，此事与公司日常经营无关", R.NEGATIVE),
    ("甲公司：否认收购传闻", "", None),
    ("甲公司：中标传闻不实", "", None),
    ("甲公司：未筹划重组，收购传闻不实", "", None),
    ("甲公司：公司不存在退市风险", "", None),
    ("甲公司：最近三年未受到行政处罚", "", None),
    ("甲公司：起诉乙公司获法院立案", "", None),
    ("甲公司：聘任乙为总经理", "乙未受过刑事处罚，不存在被证监会立案的情形", None),
    ("甲公司：股东暂无减持计划", "", None),
    ("甲公司：股东减持1%股份", "", R.NEGATIVE),
    ("甲公司：大股东减持1.2亿股", "", R.NEGATIVE),
    ("甲公司：前三季度净利润同比下滑30%", "", R.NEGATIVE),
    ("甲公司：2026年前瞻 收到立案告知书", "", R.NEGATIVE),  # a known speaker's title is no digest
    ("甲公司澄清：公司未因内幕交易被调查", "网传公司被证监会立案，当事人非公司员工", None),  # the body retells the rumour
    ("甲公司：可降解螺钉获医疗器械注册证", "", R.POSITIVE),
    ("甲公司：注射液获美国FDA上市许可", "", R.POSITIVE),
]


@pytest.mark.parametrize("title,body,direction", V2_CASES, ids=[c[0] for c in V2_CASES])
def test_v2_verdicts_on_recorded_failure_patterns(title: str, body: str, direction: str | None):
    verdict = R.classify(title, body).company_verdict()
    assert (verdict[0] if verdict else None) == direction


def test_digests_fire_no_rule_whatever_they_mention():
    body = "1、某公司被证监会立案。2、WTI原油跌超4%。3、某公司拟回购股份。"
    for title in ("财联社11月4日早间新闻精选", "11月3日投资避雷针：三公司遭证监会立案", "美股盘前要闻一览：某公司收购", "港股收评：恒生指数涨0.42%"):
        assert R.classify(title, body).hits == ()
    assert R.classify("", "财联社11月4日早间新闻精选 1、某公司被证监会立案").hits == ()


def test_a_severe_rule_reads_the_body_of_a_one_speaker_title_only():
    plain = "甲公司：关于收到相关文件的公告"
    assert R.classify(plain, "公司收到中国证监会下发的《立案告知书》").company_verdict() == (R.NEGATIVE, 3)
    assert R.classify(plain, "公司收到问询函").company_verdict() is None  # magnitude 2 stays on the title
    assert R.classify(plain, "x" * 400 + "立案").company_verdict() is None  # beyond the head of the body
    assert R.classify("证券板块今日走强", "某公司被立案").company_verdict() is None  # no speaker
    assert R.speaker("甲公司股份：拟回购") == "甲公司股份" and R.speaker("今日市场大涨") is None


def mini_engine(day_dir: Path, tmp_path: Path, names: dict[str, str], holdings: list[str], key: str | None = None,
                client: httpx.Client | None = None) -> A.AlertEngine:
    book = tmp_path / "mini_book.json"
    book.write_text(json.dumps({"date": DAY.isoformat(), "holdings": holdings, "watch": [], "source": "test"}), encoding="utf-8")
    frame = pl.DataFrame({"code": list(names), "name": list(names.values())})
    return A.AlertEngine(day_dir, DAY, book_path=book, names=A.NameIndex.from_frame(frame), members=members_for(tmp_path),
                         key_loader=lambda: key, llm_client=client, sleep=lambda s: None)


def one_message(day_dir: Path, title: str, codes: list[str], body: str = "") -> None:
    write_raw(day_dir, [{"source": "ths.724", "received_at": "2026-11-06T01:00:00+00:00",
                         "items": [{"item_id": "1", "title": title, "body": body, "codes": codes}]}])


SUBJECT_NAMES = {"600100": "星河制药", "600200": "云峰地产", "600300": "中信证券", "601988": "中国银行"}


@pytest.mark.parametrize("title,codes,holdings,hit", [
    ("云峰地产：被证监会立案", ["600200", "600100"], ["600100"], []),  # an unheld company tagging a held one (say, its lender)
    ("星河制药：被证监会立案", ["600100", "600200"], ["600100"], ["600100"]),
    ("云峰地产：被证监会立案 星河制药为其担保", ["600200"], ["600100"], ["600100"]),  # named in the title
    ("中信证券：下调星河制药评级", ["600300", "600100"], ["600100"], ["600100"]),  # a broker is not the subject of its own note
    ("中信证券：下调星河制药评级", ["600300", "600100"], ["600300"], []),
    ("中信证券：被证监会立案", ["600300"], ["600300"], ["600300"]),
    ("证监会：对星河制药立案调查", ["600100"], ["600100"], ["600100"]),  # another speaker keeps what the message tags
    ("中国银行业协会：多家银行被立案", ["600100"], ["600100"], ["600100"]),  # 中国银行 inside a longer name is no speaker
])
def test_who_a_company_rule_speaks_about(day_dir: Path, tmp_path: Path, title: str, codes: list[str], holdings: list[str], hit: list[str]):
    one_message(day_dir, title, codes)
    mini_engine(day_dir, tmp_path, SUBJECT_NAMES, holdings).step()
    assert [c for r in rows_of(day_dir) if r["level"] == "P0" for c in r["holdings_hit"]] == hit


def test_a_digest_naming_a_holding_never_reaches_the_llm(day_dir: Path, tmp_path: Path):
    one_message(day_dir, "财联社11月6日早间新闻精选", ["600100"], "星河制药发布公告。国务院发布新规。")
    calls: list[httpx.Request] = []
    mini_engine(day_dir, tmp_path, SUBJECT_NAMES, ["600100"], key=SECRET, client=llm_client(ok_handler(calls))).step()
    assert calls == [] and rows_of(day_dir) == []


# --- mapping --------------------------------------------------------------------------------------

def test_names_and_codes_in_text():
    idx = names_index()
    assert idx.find("星河制药与双子科技同时公告") == ["600513"]  # 双子科技 names two codes: dropped
    assert idx.find("*ST晨光发布公告") == ["600600"]
    assert idx.find("海川电子和云峰地产") == ["002451", "600001"]
    known = frozenset({"600513", "300001"})
    assert A.text_codes("代码600513，金额123456元，价格1234.567，300001", known) == ["600513", "300001"]
    assert A.text_codes("600513 与 835185", frozenset()) == ["600513"]  # no code table: A-share prefixes only
    assert A.norm_code("sh600513") == "600513" and A.norm_code("46.9999") is None


def test_message_codes_come_from_the_link_the_text_and_the_names(day_dir: Path, tmp_path: Path):
    eng = engine(day_dir, tmp_path)
    msg = A.Msg("ths.724", "1", "1@x", "海川电子(603001)公告", "云峰地产回购", ("002100", "bad"), datetime(2026, 11, 6, tzinfo=UTC), None)
    assert eng.mapped_codes(msg, R.normalize(msg.title + "\n" + msg.body)) == ("002100", "603001", "002451", "600001")


# --- levels ---------------------------------------------------------------------------------------

BOOK = A.Book(frozenset({"600513", "600001"}), frozenset({"002451"}), "2026-11-06")
MEMBERS = {"房地产开发": frozenset({"600001"}), "半导体": frozenset({"002451", "600888"}), "养殖业": frozenset({"002100"})}


def test_each_level_from_the_decision_function():
    neg_held = A.decide_levels(A.Verdict("rule", (R.NEGATIVE, 3), ("600513", "600999")), BOOK, MEMBERS)
    assert list(neg_held) == ["P0"] and neg_held["P0"]["holdings_hit"] == ["600513"]
    macro_neg = A.decide_levels(A.Verdict("rule", None, (), ((R.NEGATIVE, 2, ("房地产开发", "养殖业")),)), BOOK, MEMBERS)
    assert list(macro_neg) == ["P1"] and macro_neg["P1"]["industries"] == ["房地产开发"] and macro_neg["P1"]["holdings_hit"] == ["600001"]
    watch_neg = A.decide_levels(A.Verdict("rule", (R.NEGATIVE, 2), ("002451",)), BOOK, MEMBERS)
    assert list(watch_neg) == ["P2"] and watch_neg["P2"]["watch_hit"] == ["002451"]
    rising = A.decide_levels(A.Verdict("rule", (R.POSITIVE, 2), ("600777", "300001")), BOOK, MEMBERS)
    assert list(rising) == ["P2"] and rising["P2"]["trigger_codes"] == ["600777"]  # ChiNext is outside the main-board universe
    macro_watch = A.decide_levels(A.Verdict("rule", None, (), ((R.POSITIVE, 1, ("半导体",)),)), BOOK, MEMBERS)
    assert list(macro_watch) == ["P2"] and macro_watch["P2"]["watch_hit"] == ["002451"]
    held_up = A.decide_levels(A.Verdict("rule", (R.POSITIVE, 2), ("600513",)), BOOK, MEMBERS)
    assert list(held_up) == ["INFO"] and held_up["INFO"]["holdings_hit"] == ["600513"]
    macro_up_held = A.decide_levels(A.Verdict("rule", None, (), ((R.POSITIVE, 2, ("房地产开发",)),)), BOOK, MEMBERS)
    assert list(macro_up_held) == ["INFO"]
    assert A.decide_levels(A.Verdict("rule"), BOOK, MEMBERS) == {}  # unclassified: no alert
    both = A.decide_levels(A.Verdict("rule", (R.NEGATIVE, 3), ("600513", "002451"), ((R.NEGATIVE, 2, ("房地产开发",)),)), BOOK, MEMBERS)
    assert list(both) == ["P0", "P1", "P2"] and both["P2"]["watch_hit"] == ["002451"]


def test_replay_of_the_fixture_day_gives_every_level(day_dir: Path, tmp_path: Path):
    write_raw(day_dir, answers())
    eng = engine(day_dir, tmp_path)
    assert eng.step() == 11
    rows = rows_of(day_dir)
    assert {lv: sum(r["level"] == lv for r in rows) for lv in A.LEVELS} == {"P0": 1, "P1": 2, "P2": 5, "INFO": 2}
    p0 = by(rows, "ths.724", "101")["P0"]
    assert p0["rules"] == ["N-REG-01"] and p0["holdings_hit"] == ["600513"] and p0["method"] == "rule" and p0["backlog"] is True
    assert p0["received_at"] == "2026-11-06T08:00:10+08:00" and p0["alert_ts"] and p0["direction"] == "negative"
    assert p0["codes"] == ["600513"] and p0["magnitude"] == 3 and p0["llm"] == "not_needed" and p0["holdings_date"] == "2026-11-06"
    assert by(rows, "ths.724", "102")["P2"]["trigger_codes"] == ["600777"]  # rising, not held
    assert by(rows, "ths.724", "103")["P2"]["watch_hit"] == ["002451"]  # falling, but watched, not held
    oil = by(rows, "ths.724", "105")
    assert set(oil) == {"P1", "P2"} and oil["P1"]["holdings_hit"] == ["000501"] and oil["P1"]["industries"] == ["航空机场"]
    assert oil["P2"]["watch_hit"] == ["601001"] and oil["P2"]["direction"] == "positive"
    assert by(rows, "em.724", "201")["P1"]["industries"] == ["养殖业"]  # pork price drop touches a held farmer
    assert set(by(rows, "ths.724", "104")) == {"INFO"}  # rate cut: positive for a held industry, nothing to act on
    assert set(by(rows, "em.724", "203")) == {"INFO"}  # ChiNext buyback
    cls = by(rows, "cls.telegraph", "301")["P2"]
    assert cls["watch_hit"] == ["603001"] and cls["codes"] == ["603001"]  # code and name found in the text
    assert not any(r["item_id"] in ("106", "107") for r in rows)  # neutral and excluded: no alert
    assert by(rows, "em.724", "202")["P2"]["source"] == "em.724"  # the same words from another source alert again
    summary = json.loads((day_dir / "alerts_summary.json").read_text(encoding="utf-8"))
    assert summary["levels"] == {"P0": 1, "P1": 2, "P2": 5, "INFO": 2} and summary["methods"] == {"rule": 10}
    assert summary["sources"] == {"cls.telegraph": 1, "em.724": 3, "ths.724": 6} and summary["holdings_date"] == "2026-11-06"
    assert summary["llm"]["calls"] == 0 and summary["llm"]["available"] is False and summary["llm"]["skips"] == {"unavailable": 1}


def test_point_in_time_membership_keeps_a_future_joiner_out(tmp_path: Path):
    m = members_for(tmp_path)
    assert m["半导体"] == {"002451"}  # 603001 joins in December
    assert "300001" not in m.get("软件开发", frozenset())  # ChiNext is not main board


# --- dedup, restart, incremental reading ----------------------------------------------------------

def test_restart_does_not_alert_again(day_dir: Path, tmp_path: Path):
    write_raw(day_dir, answers())
    engine(day_dir, tmp_path).step()
    before = (day_dir / "alerts.jsonl").read_bytes()
    again = engine(day_dir, tmp_path)
    assert again.step() == 0 and (day_dir / "alerts.jsonl").read_bytes() == before
    (day_dir / "alerts_state.json").unlink()  # offsets lost: the raw files are read again from the start
    (day_dir / "alerts_seen.txt").unlink()  # and the neutral messages are forgotten; the alert rows still stop duplicates
    third = engine(day_dir, tmp_path)
    third.step()
    assert (day_dir / "alerts.jsonl").read_bytes() == before
    assert json.loads((day_dir / "alerts_summary.json").read_text(encoding="utf-8"))["levels"]["P0"] == 1


def test_reading_is_incremental_and_revisions_are_new_versions(day_dir: Path, tmp_path: Path):
    write_raw(day_dir, answers())
    eng = engine(day_dir, tmp_path)
    assert eng.step() == 11 and eng.step() == 0
    offsets = dict(eng.state["raw_offsets"])
    write_raw(day_dir, answers("answers_late.json"))
    assert eng.step() == 2  # item 204 and the new revision of 301; nothing is read twice
    assert all(eng.state["raw_offsets"][k] > offsets[k] for k in ("em.724.jsonl.gz", "cls.telegraph.jsonl.gz")) and eng.state["raw_offsets"]["ths.724.jsonl.gz"] == offsets["ths.724.jsonl.gz"]
    rows = rows_of(day_dir)
    assert by(rows, "em.724", "204")["P0"]["holdings_hit"] == ["000501"]
    assert by(rows, "em.724", "204")["P0"]["rules"] == ["N-HALT-01", "N-ACCIDENT-01"]
    revised = [r for r in rows if r["source"] == "cls.telegraph" and r["item_id"] == "301"]
    assert len(revised) == 2 and revised[0]["revision_id"] != revised[1]["revision_id"]
    assert revised[1]["direction"] == "negative" and "N-INQ-01" in revised[1]["rules"]  # the revision adds an inquiry letter


def test_a_member_still_being_written_waits_for_the_next_cycle(day_dir: Path, tmp_path: Path):
    write_raw(day_dir, answers()[:1])
    path = day_dir / "raw" / "ths.724.jsonl.gz"
    buf = io.BytesIO()
    line = json.dumps({"received_at": "2026-11-06T00:05:00+00:00", "items": [
        {"item_id": "900", "title": "公司收到问询函", "body": "", "codes": ["600513"]}]}, ensure_ascii=False) + "\n"
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(line.encode("utf-8"))
    member = buf.getvalue()
    eng = engine(day_dir, tmp_path)
    assert eng.step() == 2
    size = path.stat().st_size
    with path.open("ab") as fh:
        fh.write(member[:len(member) // 2])
    assert eng.step() == 0 and eng.state["raw_offsets"]["ths.724.jsonl.gz"] == size  # cut off: left alone
    with path.open("ab") as fh:
        fh.write(member[len(member) // 2:])
    assert eng.step() == 1 and by(rows_of(day_dir), "ths.724", "900")["P0"]["rules"] == ["N-INQ-01"]


def test_parquet_parts_are_the_fallback_when_there_are_no_raw_files(day_dir: Path, tmp_path: Path):
    rows = []
    for a in answers():
        for it in a["items"]:
            rows.append({"source_id": a["source"], "item_id": it["item_id"], "revision_id": A.revision_id(it["item_id"], it["title"], it["body"]),
                         "title": it["title"], "body": it["body"], "codes": it["codes"], "received_at": datetime.fromisoformat(a["received_at"])})
    (day_dir / "messages").mkdir()
    pl.DataFrame(rows, schema_overrides={"received_at": pl.Datetime("ms", "UTC")}).write_parquet(day_dir / "messages" / "part-080000-0001.parquet")
    (day_dir / "messages" / "part-080500-0002.parquet").write_bytes(b"cut off by a kill")
    eng = engine(day_dir, tmp_path)
    assert eng.step() == 11  # the repeats of 103 and 106 share their revision with the first answer
    assert sum(r["level"] == "P0" for r in rows_of(day_dir)) == 1 and eng.state["parts_done"] == ["part-080000-0001.parquet"]
    assert eng.step() == 0


def test_missing_day_directory_is_not_created(tmp_path: Path):
    eng = A.AlertEngine(tmp_path / "nope", DAY, names=A.NameIndex(), members={}, key_loader=lambda: None)
    assert eng.step() == 0 and not (tmp_path / "nope").exists()


# --- holdings -------------------------------------------------------------------------------------

def test_missing_old_and_broken_holdings_are_empty_with_a_warning(day_dir: Path, tmp_path: Path):
    write_raw(day_dir, answers())
    eng = engine(day_dir, tmp_path, book=tmp_path / "no-such-book.json")
    eng.step()
    summary = json.loads((day_dir / "alerts_summary.json").read_text(encoding="utf-8"))
    assert summary["holdings"] == 0 and summary["watch"] == 0 and summary["holdings_date"] is None
    assert any("missing" in w for w in summary["warnings"]) and summary["levels"]["P0"] == 0 and summary["levels"]["P1"] == 0
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"date": "2026-10-20", "holdings": ["600513"], "watch": []}), encoding="utf-8")
    book = A.load_book(DAY, old)
    assert book.holdings == frozenset() and book.date == "2026-10-20" and "old" in book.warnings[0]
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    assert A.load_book(DAY, broken).holdings == frozenset() and "unreadable" in A.load_book(DAY, broken).warnings[0]
    recent = tmp_path / "recent.json"  # the book of the previous Friday still serves Monday
    recent.write_text(json.dumps({"date": "2026-11-02", "holdings": ["sh600513", "x"], "watch": ["002451"]}), encoding="utf-8")
    assert A.load_book(DAY, recent).holdings == frozenset({"600513"})
    nodate = tmp_path / "nodate.json"
    nodate.write_text(json.dumps({"holdings": ["600513"]}), encoding="utf-8")
    assert A.load_book(DAY, nodate).holdings == frozenset()
    newer = tmp_path / "newer.json"
    newer.write_text(json.dumps({"date": "2026-11-09", "holdings": ["600513"]}), encoding="utf-8")
    assert A.load_book(DAY, newer).holdings == frozenset({"600513"}) and A.load_book(DAY, newer).warnings


def test_default_holdings_path_follows_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("PULSE_HOLDINGS_FILE", raising=False)
    assert A.holdings_path() == A.settings.data_dir / "holdings" / "book.json"
    monkeypatch.setenv("PULSE_HOLDINGS_FILE", str(tmp_path / "x.json"))
    assert A.holdings_path() == tmp_path / "x.json"


# --- LLM ------------------------------------------------------------------------------------------

LLM_ANSWER = {"direction": "negative", "magnitude": 3, "codes": ["600513", "999999"], "industries": ["化学制药", "没有这个行业"],
              "reason": "test"}


def llm_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_handler(calls: list[httpx.Request], payload: dict | str = LLM_ANSWER):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return httpx.Response(200, json={"output_text": text, "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}})
    return handler


def macro_day(day_dir: Path, titles: list[str], codes: list[str] | None = None) -> None:
    items = [{"item_id": str(i), "title": t, "body": "", "codes": codes or []} for i, t in enumerate(titles)]
    write_raw(day_dir, [{"source": "ths.724", "received_at": "2026-11-06T01:00:00+00:00", "items": items}])


def test_llm_judges_a_policy_message_without_rules(day_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PULSE_ALERT_MODEL", "gpt-test-model")
    macro_day(day_dir, ["国务院发布医药行业新规"])
    calls: list[httpx.Request] = []
    sleeps: list[float] = []
    eng = engine(day_dir, tmp_path, key=SECRET, client=llm_client(ok_handler(calls)), sleeps=sleeps)
    eng.step()
    assert len(calls) == 1
    req = calls[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.openai.com/v1/responses" and req.headers["authorization"] == f"Bearer {SECRET}"
    assert body["model"] == "gpt-test-model" and "JSON" in body["instructions"] and "国务院发布医药行业新规" in body["input"]
    assert "600513" in body["input"] and "星河制药" in body["input"]  # the holdings and watch list are the candidates
    levels = {r["level"]: r for r in rows_of(day_dir)}
    assert set(levels) == {"P0", "P1"}  # affected code 600513 (a holding) and industry 化学制药 (contains it)
    assert levels["P0"]["method"] == "llm" and levels["P0"]["llm_model"] == "gpt-test-model" and levels["P0"]["rules"] == []
    assert levels["P0"]["holdings_hit"] == ["600513"] and levels["P0"]["magnitude"] == 3 and levels["P1"]["industries"] == ["化学制药"]
    usage = [json.loads(x) for x in (day_dir / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert usage[0]["total_tokens"] == 120 and usage[0]["status"] == "ok" and set(usage[0]) == {
        "ts", "ref", "model", "status", "input_tokens", "output_tokens", "total_tokens"}
    assert "国务院" not in (day_dir / "llm_usage.jsonl").read_text(encoding="utf-8")  # no prompt text in the usage log


def test_llm_runs_for_a_holding_without_a_rule_and_neutral_means_no_alert(day_dir: Path, tmp_path: Path):
    macro_day(day_dir, ["星河制药召开投资者交流会"], codes=["600513"])
    calls: list[httpx.Request] = []
    eng = engine(day_dir, tmp_path, key=SECRET, client=llm_client(ok_handler(calls, {"direction": "neutral", "magnitude": 1, "codes": [], "industries": []})))
    eng.step()
    assert len(calls) == 1 and rows_of(day_dir) == []
    macro_day(day_dir, ["一条没有关键词的普通快讯"])  # neither touching a holding nor macro: never sent to the model
    assert eng.step() == 1 and len(calls) == 1


def test_llm_not_used_when_a_rule_fired(day_dir: Path, tmp_path: Path):
    write_raw(day_dir, answers())
    calls: list[httpx.Request] = []
    engine(day_dir, tmp_path, key=SECRET, client=llm_client(ok_handler(calls))).step()
    assert len(calls) == 1  # only item 107 (excluded sell-down on a holding) had no rule and touched a holding


def test_llm_cap_pace_and_malformed_answers(day_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PULSE_ALERT_LLM_CAP", "2")
    macro_day(day_dir, [f"国务院第{i}号新规政策" for i in range(4)])
    calls: list[httpx.Request] = []
    sleeps: list[float] = []
    eng = engine(day_dir, tmp_path, key=SECRET, client=llm_client(ok_handler(calls, {"direction": "positive", "magnitude": 2, "codes": ["002451"]})), sleeps=sleeps)
    eng.step()
    assert len(calls) == 2 and eng.llm.counters["calls"] == 2
    assert len(sleeps) == 1 and 1.5 < sleeps[0] <= A.LLM_PACE  # one call per 2 s
    summary = json.loads((day_dir / "alerts_summary.json").read_text(encoding="utf-8"))
    assert summary["llm"]["calls"] == 2 and summary["llm"]["cap"] == 2 and summary["llm"]["skips"] == {"cap": 2}
    assert [r["level"] for r in rows_of(day_dir)] == ["P2", "P2"] and all(r["llm"] == "ok" for r in rows_of(day_dir))


def test_five_failures_in_a_row_disable_the_llm_for_the_day(day_dir: Path, tmp_path: Path):
    macro_day(day_dir, [f"国务院第{i}号新规政策" for i in range(8)])
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {SECRET}"}})
    eng = engine(day_dir, tmp_path, key=SECRET, client=llm_client(handler))
    eng.step()
    assert len(calls) == 5 and eng.llm.counters["failures"] == 5 and eng.llm.counters["disabled"] is True
    summary = json.loads((day_dir / "alerts_summary.json").read_text(encoding="utf-8"))
    assert summary["llm"]["disabled"] is True and summary["llm"]["skips"] == {"disabled": 3}
    restarted = engine(day_dir, tmp_path, key=SECRET, client=llm_client(handler))
    assert restarted.llm.status() == "disabled"  # the breaker survives a restart
    usage = [json.loads(x) for x in (day_dir / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [u["status"] for u in usage] == ["http_401"] * 5


def test_a_success_resets_the_failure_streak_and_bad_json_counts_as_a_failure(day_dir: Path, tmp_path: Path):
    macro_day(day_dir, [f"国务院第{i}号新规政策" for i in range(3)])
    answers_iter = iter(["not json at all", {"direction": "positive", "magnitude": 1, "codes": ["002451"]}, {"direction": "bogus"}])

    def handler(req: httpx.Request) -> httpx.Response:
        a = next(answers_iter)
        return httpx.Response(200, json={"output_text": a if isinstance(a, str) else json.dumps(a)})
    eng = engine(day_dir, tmp_path, key=SECRET, client=llm_client(handler))
    eng.step()
    assert eng.llm.counters["failures"] == 2 and eng.llm.counters["consecutive"] == 1 and not eng.llm.counters["disabled"]


def test_http_exception_is_a_failure(day_dir: Path, tmp_path: Path):
    macro_day(day_dir, ["国务院发布新规"])

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=req)
    eng = engine(day_dir, tmp_path, key=SECRET, client=llm_client(handler))
    eng.step()
    assert eng.llm.counters["failures"] == 1 and rows_of(day_dir) == []
    assert json.loads((day_dir / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines()[0])["status"] == "error_ConnectTimeout"


def test_without_a_key_the_llm_is_skipped_and_recorded(day_dir: Path, tmp_path: Path):
    macro_day(day_dir, ["国务院发布新规政策", "星河制药召开交流会"], codes=None)
    eng = engine(day_dir, tmp_path, key=None)
    eng.step()
    summary = json.loads((day_dir / "alerts_summary.json").read_text(encoding="utf-8"))
    assert summary["llm"]["available"] is False and summary["llm"]["calls"] == 0 and summary["llm"]["skips"] == {"unavailable": 2}  # a policy message and a holding message
    assert not (day_dir / "llm_usage.jsonl").exists()


def test_rows_say_the_llm_was_not_needed_when_a_rule_fired(day_dir: Path, tmp_path: Path):
    write_raw(day_dir, answers())
    eng = engine(day_dir, tmp_path, key=None)
    eng.step()
    assert all(r["llm"] == "not_needed" for r in rows_of(day_dir))  # rows only exist for rule hits here


def test_api_key_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PULSE_SECRETS_DIR", str(tmp_path))
    assert A.read_api_key() is None
    (tmp_path / "openai.key").write_bytes(f"  {SECRET}  \nsecond line\n".encode())
    assert A.read_api_key() == SECRET
    monkeypatch.setenv("OPENAI_API_KEY", " sk-from-env \n")
    assert A.read_api_key() == "sk-from-env"  # the environment wins
    monkeypatch.delenv("OPENAI_API_KEY")
    (tmp_path / "openai.key").write_bytes(b"")
    assert A.read_api_key() is None


def test_the_key_appears_in_no_output_and_no_log(day_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    macro_day(day_dir, [f"国务院第{i}号新规政策" for i in range(7)])
    seen_auth: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers["authorization"])
        return httpx.Response(500, text=f"upstream failure for key {SECRET}")
    eng = A.AlertEngine(day_dir, DAY, book_path=FIX / "book.json", names=names_index(), members=members_for(tmp_path),
                        llm_client=llm_client(handler), sleep=lambda s: None)  # default key loader: the environment
    eng.step()
    assert seen_auth and all(a == f"Bearer {SECRET}" for a in seen_auth)  # the key was really used
    files = [p for p in day_dir.rglob("*") if p.is_file()]
    assert {p.name for p in files} >= {"alerts_summary.json", "alerts_state.json", "llm_usage.jsonl"}
    assert all(SECRET.encode() not in p.read_bytes() for p in files)
    assert SECRET not in caplog.text and SECRET not in json.dumps(eng.state)
    key_file = tmp_path / "secrets"
    key_file.mkdir()
    (key_file / "openai.key").write_bytes(SECRET.encode())
    monkeypatch.delenv("OPENAI_API_KEY")
    eng2 = A.AlertEngine(day_dir, DAY, names=names_index(), members={}, llm_client=llm_client(handler), sleep=lambda s: None)
    eng2.write_summary()
    assert SECRET.encode() not in (day_dir / "alerts_summary.json").read_bytes()


# --- output and process ---------------------------------------------------------------------------

def test_latest_alerts_newest_first_and_filtered(day_dir: Path, tmp_path: Path):
    write_raw(day_dir, answers())
    write_raw(day_dir, answers("answers_late.json"))
    engine(day_dir, tmp_path).step()
    p0 = A.latest_alerts("2026-11-06", level="P0")
    assert [r["item_id"] for r in p0] == ["204", "101"]
    assert len(A.latest_alerts(DAY, n=3)) == 3 and A.latest_alerts("2026-11-06", n=0) == []
    assert A.latest_alerts("2026-11-06", level="p1")[0]["level"] == "P1"
    assert A.latest_alerts("2026-01-01") == []
    with (day_dir / "alerts.jsonl").open("ab") as fh:
        fh.write(b'{"level": "P0", "half a li')  # a row being written right now
    assert [r["item_id"] for r in A.latest_alerts(DAY, "P0")] == ["204", "101"]


def test_alert_files_use_lf_and_utf8(day_dir: Path, tmp_path: Path):
    write_raw(day_dir, answers())
    engine(day_dir, tmp_path).step()
    for name in ("alerts.jsonl", "alerts_summary.json", "alerts_state.json", "alerts_seen.txt"):
        data = (day_dir / name).read_bytes()
        assert b"\r" not in data
    assert "星河制药收到证监会立案告知书" in (day_dir / "alerts.jsonl").read_text(encoding="utf-8")  # real UTF-8, not unicode escapes
    assert all(len(r["title"]) <= 80 for r in rows_of(day_dir))


def test_once_cli_replays_a_day(day_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    write_raw(day_dir, answers())
    assert A.main(["once", "--date", "2026-11-06"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # no securities table in the test warehouse: item 101 names its company only in the text, so it stays INFO
    assert out["levels"] == {"P0": 0, "P1": 2, "P2": 5, "INFO": 3}
    assert A.main(["once", "--date", "2026-11-07"]) == 1  # nothing recorded that day


def test_run_exits_by_2230_and_skips_non_trading_days(day_dir: Path, monkeypatch: pytest.MonkeyPatch):
    write_raw(day_dir, answers()[:1])
    clock = {"t": datetime(2026, 11, 6, 22, 0, tzinfo=N.CST)}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += timedelta(seconds=seconds)
    monkeypatch.setattr(A, "_now", lambda: clock["t"])
    monkeypatch.setattr(A, "_sleep", sleep)
    assert A.run() == 0
    assert clock["t"].time() >= A.STOP_AT and sum(sleeps) == 1800 and max(sleeps) == A.POLL_SECONDS
    assert (day_dir / "alerts_summary.json").exists()
    clock["t"] = datetime(2026, 11, 22, 10, 0, tzinfo=N.CST)  # a Sunday
    sleeps.clear()
    assert A.run() == 0 and sleeps == []
    clock["t"] = datetime(2026, 11, 6, 23, 0, tzinfo=N.CST)  # started after the stop time: one pass, then exit
    assert A.run() == 0 and sleeps == []


def test_a_failing_cycle_does_not_end_the_day(day_dir: Path, monkeypatch: pytest.MonkeyPatch):
    clock = {"t": datetime(2026, 11, 6, 22, 29, tzinfo=N.CST)}
    monkeypatch.setattr(A, "_now", lambda: clock["t"])
    monkeypatch.setattr(A, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + timedelta(seconds=s)))
    boom = {"n": 0}

    def step(self: A.AlertEngine) -> int:
        boom["n"] += 1
        raise RuntimeError("boom")
    monkeypatch.setattr(A.AlertEngine, "step", step)
    assert A.run() == 0 and boom["n"] == 3  # 22:29:00, 22:29:30, 22:30:00
