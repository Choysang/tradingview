"""Helpers for the alert LLM fallback tests: the old system's news fixtures (copied to ``fixtures/alert_llm``) as claims, a user
who holds / watches the old book, and a mocked Responses-API transport. Nothing here touches the network."""
from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from pulse.adapters.models.alert_llm import AlertLlmClient
from pulse.application import alert_llm_service as LS
from pulse.application import alert_service as AS
from pulse.application import portfolio_service as PS
from pulse.domain.intelligence import claims as claims_mod
from pulse.domain.intelligence.feeds import NewsRecord

from . import support as S

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "alert_llm"
SECRET = "sk-TESTSECRET-0123456789"
UTC = timezone.utc


def fixture_messages(*names: str) -> list[dict]:
    """Items of the old answer files (``answers.json``, ``answers_late.json``), one dict per item_id."""
    out: dict[str, dict] = {}
    for name in names or ("answers.json", "answers_late.json"):
        for batch in json.loads((FIX / name).read_text(encoding="utf-8")):
            for it in batch["items"]:
                out.setdefault(it["item_id"], {"item_id": it["item_id"], "title": it["title"], "body": it["body"], "codes": it["codes"]})
    return list(out.values())


def book() -> dict:
    return json.loads((FIX / "book.json").read_text(encoding="utf-8"))


def securities() -> dict[str, str]:
    with (FIX / "securities.csv").open(encoding="utf-8", newline="") as fh:
        return {r["code"]: r["name"] for r in csv.DictReader(fh)}


def vendor_ref(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


def rule_view(msg: dict) -> claims_mod.Candidate:
    """What the claim rules (the pulse3 pipeline, not the old news_rules) make of a message."""
    rec = NewsRecord("fixture", msg["item_id"], msg["title"], msg["body"], None, "unknown", tuple(vendor_ref(c) for c in msg["codes"]), False)
    return claims_mod.extract_rules(rec)[0]


class World:
    """Instruments of the old securities list; a user who holds / watches the old book (``fixtures/alert_llm/book.json``)."""

    def __init__(self, conn, principal: uuid.UUID, now: datetime):
        self.conn, self.principal, self.now = conn, principal, now
        self.ids = {code: S.seed_instrument(conn, code, name) for code, name in securities().items()}
        b = book()
        self.held = [self.ids[c] for c in b["holdings"] if c in self.ids]
        self.watch = [self.ids[c] for c in b["watch"] if c in self.ids]
        acct = S.open_account(conn, positions={str(i): {"quantity": 1000, "cost": "10000"} for i in self.held})
        PS.grant(conn, principal, acct, "read", now - timedelta(days=2))
        wl = uuid.uuid4()
        conn.execute("INSERT INTO ui.watchlist(watchlist_id,principal_id,watchlist_version,display_name,created_at,updated_at) VALUES (%s,%s,1,'w',now(),now())", (wl, principal))
        for n, iid in enumerate(self.watch, 1):
            conn.execute("INSERT INTO ui.watchlist_member(watchlist_id,instrument_id,sort_position,added_at) VALUES (%s,%s,%s,now())", (wl, iid, n))
        AS.init_default_rules(conn, principal, now - timedelta(days=1))

    def claim(self, msg: dict, *, minutes_ago: int = 3, category: str | None = None) -> dict:
        cand = rule_view(msg)
        return S.add_claim(self.conn, headline=msg["title"] or msg["body"][:60], category=category or cand.category or "social.attention_sentiment",
                           polarity=cand.polarity_hint, instruments=[self.ids[c] for c in msg["codes"] if c in self.ids],
                           available_at=self.now - timedelta(minutes=minutes_ago))

    def scan(self, llm=None, now: datetime | None = None) -> dict:
        return AS.scan(self.conn, now or self.now, principals=[self.principal], since=self.now - timedelta(hours=1), until=self.now, llm=llm)

    def intents(self) -> list[dict]:
        return self.conn.execute(
            "SELECT i.tier, i.dedup_key, a.body FROM notification.alert_intent i JOIN ops.ext_artifact a ON a.ref=i.evidence_artifact"
            " WHERE i.principal_id=%s ORDER BY i.tier, i.dedup_key", (self.principal,)).fetchall()


def msg(item_id: str, title: str, codes: list[str] | None = None, body: str = "") -> dict:
    return {"item_id": item_id, "title": title, "body": body, "codes": codes or []}


ANSWER = {"direction": "negative", "magnitude": 3, "confidence": 0.8, "codes": ["600513", "999999"], "industries": ["没有这个行业"], "reason": "test"}


class Transport:
    """Mocked Responses API: records requests, answers with ``payload`` (dict -> JSON text, str -> as is) or a fixed status."""

    def __init__(self, payload: dict | str | list = ANSWER, status: int = 200, body_text: str | None = None):
        self.calls: list[httpx.Request] = []
        self.payload, self.status, self.body_text = payload, status, body_text
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.status != 200:
            return httpx.Response(self.status, text=self.body_text if self.body_text is not None else json.dumps({"error": {"message": f"bad key {SECRET}"}}))
        p = self.payload.pop(0) if isinstance(self.payload, list) else self.payload
        text = p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)
        return httpx.Response(200, json={"output_text": text, "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}})

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.calls]


def fallback(tmp_path: Path, transport: Transport, *, cap: int | None = None, pace: float = 0.0, sleeps: list[float] | None = None,
             model: str = "gpt-test-model", max_consecutive: int = LS.MAX_CONSECUTIVE_FAILURES, budget: float = 3600.0) -> LS.AlertFallback:
    key = tmp_path / "openai.key"
    key.write_bytes(f"{SECRET}\n".encode("utf-8"))
    client = AlertLlmClient(key_file=key, model=model, transport=transport.transport)
    return LS.AlertFallback(client, cap=cap, pace=pace, sleep=(sleeps.append if sleeps is not None else (lambda s: None)), max_consecutive=max_consecutive, budget_seconds=budget)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def allow_test_models(monkeypatch):
    """The alert tests use made-up model names; the shared allow-list (``PULSE_LLM_ALLOWED_MODELS``, default gpt-6-luna only) must let them through. Import this fixture into a test module."""
    monkeypatch.setenv("PULSE_LLM_ALLOWED_MODELS", "gpt-6-luna,gpt-test-model,m2")
