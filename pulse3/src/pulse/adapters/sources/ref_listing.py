"""Native listing-date adapter: SSE main-board A list (JSON) + SZSE A-share list (xlsx) (Step 2, lane D).

Why: the old system took ``list_date`` from the exchanges through AkShare (``stock_info_sh_name_code("主板A股")``
and ``stock_info_sz_name_code("A股列表")``, two requests, ``backend/app/warehouse/listing.py``). The same two public
endpoints are called directly here: ``query.sse.com.cn/sseQuery/commonQuery.do`` (JSON, needs the SSE Referer) and
``www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx`` (an .xlsx workbook, parsed with the standard library: the xlsx
container is a zip of XML parts, so no spreadsheet dependency is added). The old table, and so this one, holds the
Shanghai MAIN board and every Shenzhen A share (main + ChiNext); STAR and BSE are not in the legacy table.

Invariants: raw bytes untouched; an HTML / redirect answer is a block; a list shorter than ``MIN_ROWS`` is not trusted
(the old guard) and is rejected by the service, never published; codes are zero-padded 6-digit strings; a date that
cannot be read becomes NULL (never today's date, never a guess).
Not covered: STAR/BSE lists, delisting dates, name changes.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

from pulse.domain.evidence.announcements import NotData, SchemaChanged

from .http import Capture, RateLimitedClient
from .ref_market import guard_not_blocked

SOURCE_ID = "exchange_lists"
EP_SSE, EP_SZSE, OUT_LISTINGS = "sse_stock_list", "szse_stock_list", "listings"
SCHEMA_VERSION = "exchange-lists-20261119"
MAPPING_VERSION = "1.0.0"
MIN_ROWS = 1000
SSE_URL = ("https://query.sse.com.cn/sseQuery/commonQuery.do?STOCK_TYPE=1&REG_PROVINCE=&CSRC_CODE=&STOCK_CODE="
           "&sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L&COMPANY_STATUS=2,4,5,7,8&type=inParams&isPagination=true"
           "&pageHelp.cacheSize=1&pageHelp.beginPage=1&pageHelp.pageSize=10000&pageHelp.pageNo=1&pageHelp.endPage=1")
SSE_HEADERS = {"Referer": "https://www.sse.com.cn/assortment/stock/list/share/"}
SZSE_URL = "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1"

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_EXCEL_EPOCH = date(1899, 12, 30)


def parse_date(value: object) -> date | None:
    """``19901219`` / ``1990-12-19`` / ``1990-12-19 00:00:00`` / an Excel serial -> date; anything else None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if re.fullmatch(r"\d{5}(\.\d+)?", text):
        return _EXCEL_EPOCH + timedelta(days=int(float(text)))
    return None


def parse_sse(body: bytes) -> list[dict]:
    """SSE JSON -> ``[{code, list_date}]``."""
    try:
        doc = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotData("sse_list_invalid_json") from exc
    rows = doc.get("result") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise SchemaChanged("sse_list_no_result_array")
    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict) or "A_STOCK_CODE" not in r or "LIST_DATE" not in r:
            raise SchemaChanged("sse_list_row_shape")
        code = str(r["A_STOCK_CODE"]).strip().zfill(6)
        if re.fullmatch(r"\d{6}", code):
            out[code] = {"code": code, "list_date": parse_date(r["LIST_DATE"])}
    return list(out.values())


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str | None:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iterfind(".//m:t", _NS))
    v = cell.find("m:v", _NS)
    if v is None or v.text is None:
        return None
    return shared[int(v.text)] if kind == "s" else v.text


def read_xlsx_rows(body: bytes) -> list[list[str | None]]:
    """First worksheet of an .xlsx as a list of rows (cell text, ``None`` for empty), standard library only."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as exc:
        raise NotData("szse_list_not_an_xlsx") from exc
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iterfind(".//m:t", _NS)) for si in root.iterfind("m:si", _NS)]
    sheets = sorted(n for n in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    if not sheets:
        raise SchemaChanged("szse_list_no_worksheet")
    rows: list[list[str | None]] = []
    for row in ElementTree.fromstring(zf.read(sheets[0])).iterfind(".//m:sheetData/m:row", _NS):
        cells: dict[int, str | None] = {}
        for c in row.iterfind("m:c", _NS):
            letters = re.match(r"[A-Z]+", c.get("r", "A"))
            col = 0
            for ch in letters.group(0) if letters else "A":
                col = col * 26 + ord(ch) - 64
            cells[col - 1] = _cell_text(c, shared)
        rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
    return rows


def parse_szse(body: bytes) -> list[dict]:
    """SZSE A-share workbook -> ``[{code, list_date}]`` (columns ``A股代码`` and ``A股上市日期`` by header name)."""
    rows = read_xlsx_rows(body)
    if not rows:
        raise SchemaChanged("szse_list_empty_sheet")
    header = [h or "" for h in rows[0]]
    try:
        ci, di = header.index("A股代码"), header.index("A股上市日期")
    except ValueError as exc:
        raise SchemaChanged("szse_list_header_changed") from exc
    out: dict[str, dict] = {}
    for r in rows[1:]:
        raw = r[ci] if ci < len(r) else None
        if raw is None:
            continue
        code = str(raw).split(".")[0].strip().zfill(6)
        if re.fullmatch(r"\d{6}", code):
            out[code] = {"code": code, "list_date": parse_date(r[di] if di < len(r) else None)}
    return list(out.values())


class ListingAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def sse(self) -> Capture:
        cap = self.client.request(source_id=SOURCE_ID, endpoint_id=EP_SSE, request_key="sse:main_a", url=SSE_URL, headers=SSE_HEADERS)
        return guard_not_blocked(self.client, cap)

    def szse(self) -> Capture:
        cap = self.client.request(source_id=SOURCE_ID, endpoint_id=EP_SZSE, request_key="szse:a_list", url=SZSE_URL)
        return guard_not_blocked(self.client, cap)
