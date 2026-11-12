"""PEX16 (SSE over the update log) and the event tape on real PostgreSQL."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.postgres.outbox import PgOutbox, encode_cursor, stream_name_for
from pulse.adapters.sources.http import RateLimitedClient
from pulse.adapters.sources.sina import SinaAdapter
from pulse.application import account_service as A
from pulse.application import market_today_service as MT
from pulse.application.data_service import DataService
from pulse.application.ports.delivery import OutboxEvent
from pulse.domain.market_state import daily

pytestmark = pytest.mark.pg
SCOPE = {"scope_kind": "market", "scope_key": "main", "membership_snapshot_id": None}
STREAM = stream_name_for("release_slot", "main")


def frames(text: str) -> list[dict]:
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        f = {"id": None, "event": None, "data": None, "reason": None}
        for line in block.split("\n"):
            if line.startswith(": reason="):
                f["reason"] = line[9:]
            elif line.startswith("id: "):
                f["id"] = line[4:]
            elif line.startswith("event: "):
                f["event"] = line[7:]
            elif line.startswith("data: "):
                f["data"] = json.loads(line[6:])
        if f["data"] is not None:
            out.append(f)
    return out


def open_stream(client, last_event_id: str | None = None, cursor: str | None = None, versions=("3.0.0",), scope=SCOPE):
    params = {"scope": json.dumps(scope), "accepted_schema_versions": json.dumps(list(versions))}
    if cursor:
        params["cursor"] = cursor
    headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
    with client.stream("GET", "/v1/x/stream-registered-updates", params=params, headers=headers) as r:
        assert r.status_code == 200, r.read()
        assert r.headers["content-type"].startswith("text/event-stream")
        return frames("".join(r.iter_text()))


def append(conn, n: int, event_type: str = "decision.published", release_id: str | None = None) -> OutboxEvent:
    version = conn.execute("select coalesce(max(aggregate_version),0)+1 v from ops.outbox where aggregate_type='release_slot' and aggregate_id='main'").fetchone()["v"]
    e = OutboxEvent(event_id=uuid.uuid4(), event_type=event_type, schema_version="3.0.0", aggregate_type="release_slot", aggregate_id="main",
                    aggregate_version=version + 1000 + n, occurred_at=datetime.now(timezone.utc), producer_version="test",
                    payload={"release_id": release_id or str(uuid.uuid4()), "n": n})
    PgOutbox(conn).append(e)
    return e


def head(conn) -> str:
    return PgOutbox(conn).poll(STREAM, None).resume_cursor


# ---------------------------------------------------------------------------------------------- PEX16
def test_first_connection_resets_then_resumes_from_last_event_id_without_duplicates(client, conn, built):
    first = open_stream(client)
    assert first[0]["event"] == "reset" and first[0]["data"]["resync_required"] is True and first[0]["reason"] == "NO_CURSOR"
    contracts.check("streamRegisteredUpdatesData", first[0]["data"], contracts.EXT_ID)
    base = first[0]["id"]
    e1, e2 = append(conn, 1), append(conn, 2)
    got = open_stream(client, last_event_id=base)
    updates = [f for f in got if f["event"] == "update"]
    assert [f["data"]["notification"]["event_id"] for f in updates] == [str(e1.event_id), str(e2.event_id)]       # in log order
    for f in updates:
        contracts.check("StreamEnvelope", f["data"], contracts.EXT_ID)
        assert f["data"]["cursor"] == f["id"] and f["data"]["notification"]["event_type"] == "decision.published"
        assert f["data"]["release_id"] == f["data"]["notification"]["payload"]["release_id"] and f["data"]["scope"] == SCOPE
    # reconnect from the first event's id: only the second one, never the first again
    again = [f for f in open_stream(client, last_event_id=updates[0]["id"]) if f["event"] == "update"]
    assert [f["data"]["notification"]["event_id"] for f in again] == [str(e2.event_id)]
    # the query cursor works too and Last-Event-ID wins when both are present
    both = [f for f in open_stream(client, cursor=base, last_event_id=updates[1]["id"]) if f["event"] == "update"]
    assert both == []


def test_stream_and_poll_share_one_log(client, conn, built):
    base = head(conn)
    e = append(conn, 3)
    poll = client.get("/v1/updates", params={"slot_key": "main", "cursor": base}).json()["data"]
    sse = [f for f in open_stream(client, last_event_id=base) if f["event"] == "update"]
    assert [x["event_id"] for x in poll["events"]] == [str(e.event_id)] == [f["data"]["notification"]["event_id"] for f in sse]
    assert poll["events"][0]["payload"] == sse[0]["data"]["notification"]["payload"]


def test_expired_cursor_and_epoch_change_reset_instead_of_guessing(client, conn, built):
    old = head(conn)
    for i in range(3):
        append(conn, 10 + i)
    PgOutbox(conn).prune(STREAM, keep_last=1)                                       # the old cursor now points before the retained window
    got = open_stream(client, last_event_id=old)
    assert got[0]["event"] == "reset" and got[0]["reason"] == "CURSOR_EXPIRED" and got[0]["id"] != old
    current = head(conn)
    conn.execute("select ops.rotate_outbox_epoch(%s)", (STREAM,))                  # a rebuilt stream: old epoch cursors must resync
    assert open_stream(client, last_event_id=current)[0]["event"] == "reset"


def test_unregistered_event_types_never_ride_the_3_0_0_stream(client, conn, built):
    base = head(conn)
    append(conn, 20, event_type="alert.user.triggered")
    ok = append(conn, 21)
    got = [f for f in open_stream(client, last_event_id=base) if f["event"] == "update"]
    assert [f["data"]["notification"]["event_id"] for f in got] == [str(ok.event_id)]


def test_stream_request_validation(client):
    other = encode_cursor(stream_name_for("release_slot", "elsewhere"), "1", 0)
    r = client.get("/v1/x/stream-registered-updates", params={"scope": json.dumps(SCOPE), "accepted_schema_versions": '["3.0.0"]', "cursor": other})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CURSOR_INVALID"
    r = client.get("/v1/x/stream-registered-updates", params={"scope": json.dumps(SCOPE), "accepted_schema_versions": '["4.0.0"]'})
    assert r.status_code == 406                                                    # different schema major: never mixed
    r = client.get("/v1/x/stream-registered-updates", params={"scope": json.dumps({**SCOPE, "scope_kind": "instrument"}), "accepted_schema_versions": '["3.0.0"]'})
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNSUPPORTED_CONTEXT_SHAPE"
    r = client.get("/v1/x/stream-registered-updates", params={"scope": json.dumps(SCOPE)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_CONTRACT"


# ---------------------------------------------------------------------------------------------- tape
def hq(code: str, prev: str, last: str, ask: str, when: str) -> str:
    f = ["名称", prev, prev, last, last, prev, last, ask, "1000000", "10000000", "100", last] + ["0"] * 8 + ["0", ask] + ["0"] * 8 + ["2026-11-11", when, "00", "H|0|0"]
    f[20] = "0" if ask == "0" else "100"
    return f'var hq_str_{code}="{",".join(f)}";\n'


def sina_for(prices: dict[str, tuple[str, str]], when: str) -> SinaAdapter:
    """Sina quote answer with the given (last, ask) per sh/sz symbol; prev-close column is irrelevant (the platform uses its own raw bars)."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = "".join(hq(sym, "10.00", last, ask, when) for sym, (last, ask) in prices.items())
        return httpx.Response(200, content=body.encode("gbk"), headers={"content-type": "text/javascript; charset=GBK"})
    return SinaAdapter(RateLimitedClient(min_interval_seconds=0, transport=httpx.MockTransport(handler)))


def ids_of(conn, codes):
    return {r["instrument_id"]: r["source_symbol"] for r in conn.execute(
        "select a.instrument_id, a.source_symbol from core.instrument_alias a join core.instrument i using (instrument_id)"
        " where a.source_id='legacy_warehouse' and i.instrument_type='equity' and a.source_symbol = any(%s)", (list(codes),))}


def prev_close(conn, iid) -> Decimal:
    return conn.execute("select close_price_raw c from market.bar_projection where instrument_id=%s and timeframe='1d' and trade_date=%s"
                        " order by available_at desc limit 1", (iid, date(2026, 11, 10))).fetchone()["c"]


@pytest.fixture(scope="module")
def tape_world(world, built):
    conn = db.connect(world["dsn"], autocommit=True)
    svc = DataService(conn, LakeStore(world["lake_root"]))
    codes = {"000001": "sz000001", "600000": "sh600000", "002415": "sz002415"}
    ids = ids_of(conn, codes)
    by_code = {v: k for k, v in ids.items()}
    up = {c: daily.limit_prices(prev_close(conn, by_code[c]), Decimal("0.10"))[0] for c in codes}
    pc = {c: prev_close(conn, by_code[c]) for c in codes}
    fmt = lambda x: f"{x:.2f}"                                                               # noqa: E731
    steps = [  # (000001: seal path, 600000: surge, 002415: plunge)
        {"sz000001": (fmt(up["000001"] - Decimal("0.2")), fmt(up["000001"] - Decimal("0.1"))), "sh600000": (fmt(pc["600000"] * Decimal("1.03")), "99"),
         "sz002415": (fmt(pc["002415"] * Decimal("0.97")), "99")},
        {"sz000001": (fmt(up["000001"]), "0"), "sh600000": (fmt(pc["600000"] * Decimal("1.055")), "99"), "sz002415": (fmt(pc["002415"] * Decimal("0.945")), "99")},
        {"sz000001": (fmt(up["000001"] - Decimal("0.1")), fmt(up["000001"])), "sh600000": (fmt(pc["600000"] * Decimal("1.056")), "99"),
         "sz002415": (fmt(pc["002415"] * Decimal("0.944")), "99")},
        {"sz000001": (fmt(up["000001"]), "0"), "sh600000": (fmt(pc["600000"] * Decimal("1.056")), "99"), "sz002415": (fmt(pc["002415"] * Decimal("0.944")), "99")},
    ]
    for n, step in enumerate(steps):
        A.publish_quote_snapshot(svc, sina_for(step, f"10:0{n}:00"), ids)
    yield conn, svc, ids
    conn.close()


def test_tape_derives_every_event_kind_from_consecutive_snapshots(tape_world):
    conn, svc, ids = tape_world
    res = MT.derive_tape(svc)
    assert res["pairs"] == 3 and res["events"] >= 5
    rows = conn.execute("select payload, event_type from ops.outbox where aggregate_type='market_tape' order by aggregate_version, event_ordinal").fetchall()
    kinds = [(r["payload"]["code"], r["payload"]["event_kind"]) for r in rows]
    assert {r["event_type"] for r in rows} == {"market.tape.event"}
    assert ("000001", "seal") in kinds and ("000001", "break") in kinds and ("000001", "reseal") in kinds       # first seal, break, then a reseal
    assert kinds.index(("000001", "seal")) < kinds.index(("000001", "break")) < kinds.index(("000001", "reseal"))
    assert ("600000", "surge") in kinds and ("002415", "plunge") in kinds
    seal = next(r["payload"] for r in rows if r["payload"]["event_kind"] == "seal")
    assert seal["limit_streak"] == 1 and seal["detail"].endswith("首板") and seal["trade_date"] == "2026-11-11" and seal["tape_version"] == "tape-v1"
    assert len(seal["themes"]) <= 3 and seal["from_snapshot_id"] != seal["to_snapshot_id"]
    for r in rows:
        MT.validate_tape_payload(r["payload"])


def test_tape_is_idempotent_and_never_repeats_inside_the_cooldown(tape_world):
    conn, svc, ids = tape_world
    before = conn.execute("select count(*) n from ops.outbox where aggregate_type='market_tape'").fetchone()["n"]
    again = MT.derive_tape(svc)
    assert conn.execute("select count(*) n from ops.outbox where aggregate_type='market_tape'").fetchone()["n"] == before
    surges = conn.execute("select count(*) n from ops.outbox where aggregate_type='market_tape' and payload->>'event_kind'='surge'").fetchone()["n"]
    assert surges == 1 and again["events"] == 0


def test_tape_poll_and_stream_serve_the_same_validated_events(client, tape_world):
    r = client.get("/v1/x/a/market-tape", params={"recent": 50})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["reset_required"] is True and len(d["events"]) >= 5 and d["resume_cursor"]
    ids = [e["event_id"] for e in d["events"]]
    nxt = client.get("/v1/x/a/market-tape", params={"cursor": d["resume_cursor"]}).json()["data"]
    assert nxt["events"] == [] and nxt["reset_required"] is False
    with client.stream("GET", "/v1/x/a/market-tape/stream", headers={"Last-Event-ID": encode_cursor(stream_name_for("market_tape", "main"), d["stream_epoch"], 0)}) as s:
        got = frames("".join(s.iter_text()))
    tape = [f for f in got if f["event"] == "tape"]
    assert [f["data"]["event_id"] for f in tape] == ids                                      # full replay from position 0, in order
    assert all(f["id"] for f in tape)
    assert client.get("/v1/x/a/market-tape", params={"cursor": "garbage"}).status_code == 400
