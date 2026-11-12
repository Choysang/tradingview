"""Optional webhook push (WeCom / Feishu / DingTalk / generic) for alert intents (old site ``notify/push.py`` semantics).

Why: an alert intent is a business fact; getting it to a phone is a separate, fallible delivery attempt. This module is
the only place that talks to a chat webhook.
Invariants:
* nothing is ever sent unless a webhook URL is configured in the environment (``PULSE_PUSH_WEBHOOK`` [+ ``PULSE_PUSH_KIND``])
  or in a secrets file (``PULSE_PUSH_WEBHOOK_FILE`` or ``<PULSE_SECRETS_DIR>/push_webhook.url``: first line URL, optional second
  line kind); the URL is a credential: it is never stored in the database (only the reference), never logged, never returned
  (status shows kind and host only);
* tests never send: without an injected ``transport`` a call under pytest raises ``SendDisabled`` before any socket opens;
* a provider answer decides the state: HTTP 2xx and (for WeCom/DingTalk/Feishu) a zero error code = ``accepted`` (the
  provider took it; delivery to a phone is not observable, so never ``confirmed``); a timeout or dropped connection =
  ``unknown`` (the message may or may not have arrived); anything else = ``failed`` with a short code; the error text never
  contains the URL;
* the message states the verification status of the claim and the research-use disclaimer.
Not covered: signature-secret modes of Feishu/DingTalk bots, rate limiting beyond the retry policy of the caller.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

import httpx

KINDS = ("wecom", "feishu", "dingtalk", "generic")
KIND_NAMES = {"wecom": "企业微信", "feishu": "飞书", "dingtalk": "钉钉", "generic": "通用 Webhook"}
ENV_URL, ENV_KIND, ENV_FILE, ENV_SECRETS_DIR = "PULSE_PUSH_WEBHOOK", "PULSE_PUSH_KIND", "PULSE_PUSH_WEBHOOK_FILE", "PULSE_SECRETS_DIR"
SECRET_REFS = (f"env:{ENV_URL}", "file:push_webhook.url")
TIMEOUT_SECONDS = 10.0
MAX_CHARS = 3400            # WeCom markdown caps near 4096 bytes; Chinese text is 3 bytes per char
DISCLAIMER = "研究用途，不构成投资建议"


class SendDisabled(RuntimeError):
    """A real send was attempted where it must never happen (tests) or without configuration."""


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    kind: str

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or "?"

    def __repr__(self) -> str:                    # the URL is a credential
        return f"WebhookConfig(kind={self.kind!r}, host={self.host!r})"


@dataclass(frozen=True)
class SendResult:
    status: str                  # accepted | failed | unknown
    provider_message_id: str | None = None
    error_code: str | None = None


def _valid(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def load_config(env: Mapping[str, str] | None = None) -> WebhookConfig | None:
    """The configured webhook, or None. A malformed URL counts as not configured."""
    env = os.environ if env is None else env
    url = (env.get(ENV_URL) or "").strip()
    kind = (env.get(ENV_KIND) or "").strip().lower()
    if not url:
        path = env.get(ENV_FILE)
        if not path and env.get(ENV_SECRETS_DIR):
            path = str(Path(env[ENV_SECRETS_DIR]) / "push_webhook.url")
        if path:
            try:
                lines = [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
            except OSError:
                lines = []
            if lines:
                url = lines[0]
                kind = kind or (lines[1].lower() if len(lines) > 1 else "")
    if not url or not _valid(url):
        return None
    return WebhookConfig(url, kind if kind in KINDS else "generic")


def status(env: Mapping[str, str] | None = None) -> dict:
    cfg = load_config(env)
    if cfg is None:
        return {"configured": False, "kind": None, "host": None}
    return {"configured": True, "kind": cfg.kind, "host": cfg.host}


def message_text(alert: Mapping) -> str:
    """Plain-text body of one alert intent evidence document."""
    names = "、".join(f"{i.get('name') or i.get('code') or '未命名证券'}" for i in alert.get("instruments", [])) or "（未点名个股）"
    inds = ("；行业：" + "、".join(alert["industries"])) if alert.get("industries") else ""
    text = (f"[{alert['tier']}] {alert['headline']}\n对象：{names}{inds}\n依据：{alert.get('reason', '')}；"
            f"核验状态：{alert.get('claim_status', '未知')}（未核验不等于不实）\n{DISCLAIMER}")
    return text[:MAX_CHARS]


def build_payload(kind: str, alert: Mapping) -> dict:
    text = message_text(alert)
    if kind == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": text.replace("\n", "\n\n")}}
    if kind == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": f"Pulse {alert['tier']}", "text": text.replace("\n", "\n\n")}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"source": "pulse3", "tier": alert["tier"], "text": text, "alert": dict(alert)}


def _classify(resp: httpx.Response) -> SendResult:
    if not 200 <= resp.status_code < 300:
        return SendResult("failed", error_code=f"HTTP_{resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        return SendResult("accepted")                # generic endpoints may answer with plain text
    if isinstance(body, dict):
        code = body.get("errcode", body.get("code", body.get("StatusCode", 0)))
        if code not in (0, "0", None):
            return SendResult("failed", error_code=f"PROVIDER_{code}"[:40])
        return SendResult("accepted", str(body.get("msg_id") or body.get("msgid") or "") or None)
    return SendResult("accepted")


def send(cfg: WebhookConfig | None, alert: Mapping, *, transport: httpx.BaseTransport | None = None) -> SendResult:
    """One delivery attempt. Never raises for provider/network trouble; raises SendDisabled for a forbidden send."""
    if cfg is None:
        raise SendDisabled("no webhook configured")
    if transport is None and os.environ.get("PYTEST_CURRENT_TEST"):
        raise SendDisabled("real webhook sends are disabled under pytest")
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = client.post(cfg.url, json=build_payload(cfg.kind, alert))
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
        return SendResult("unknown", error_code="TRANSPORT_UNKNOWN")
    except httpx.HTTPError as exc:
        return SendResult("failed", error_code=type(exc).__name__[:40])
    return _classify(resp)
