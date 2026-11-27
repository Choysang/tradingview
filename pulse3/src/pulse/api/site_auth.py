"""Site login gate for public access through the HTTPS reverse proxy (Caddy on 443 -> 127.0.0.1:8200).

Why: the site used to be reachable only through an SSH tunnel to a loopback port. Public access needs a login in front of
every page and every /v1 call. The gate sits in front of the app, so the existing principal model (``auth.py``, the
single-user dev principal on the server) is unchanged behind it.

Which requests are gated:
- a request that came through a proxy (it carries ``X-Forwarded-For`` or ``Forwarded``; Caddy always sets them), or
- any request from a non-loopback client when ``PULSE_SITE_AUTH=on``.
Direct loopback requests (SSH tunnel, server scripts, tests) pass as before. A gated request with no password configured
gets 503: the gate fails closed, never open.

Credentials:
- ``PULSE_SITE_PASSWORD_FILE``: one line ``scrypt$<n>$<r>$<p>$<salt hex>$<hash hex>`` written by
  ``python -m pulse.workers.site_password``; the plain password is never stored.
- ``PULSE_SITE_SECRET_FILE``: a random HMAC key (hex). Session cookies ``pulse_site`` are signed with it and bound to the
  password hash, so a new password logs every session out.
Failed logins are limited per client address and in total (``LOCK_*``); a locked client gets 429. The total limit means a
flood of wrong passwords can lock the owner out for 15 minutes; existing sessions and the SSH tunnel still work then.
Signed-in state-changing requests must be same-origin (``same_origin``).
Not covered: WebSocket routes. ``app.middleware("http")`` sees only HTTP; the app has none today, and adding one needs a
pure ASGI gate first.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

COOKIE = "pulse_site"
SESSION_SECONDS = 30 * 24 * 3600
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
SCRYPT_MAXMEM = 128 * 1024 * 1024
MIN_PASSWORD_LEN = 12
MAX_BODY = 4096
LOCK_WINDOW = 15 * 60
LOCK_PER_CLIENT = 5
LOCK_TOTAL = 30
JSON_PREFIXES = ("/v1", "/mcp", "/healthz", "/app-config.json")


# ---------------------------------------------------------------- password hash

def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.strip().split("$")
        if kind != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


def _read(env: str) -> str | None:
    path = os.environ.get(env)
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def credentials() -> tuple[str, bytes] | None:
    """(password hash, HMAC key) or None when the gate is not configured."""
    stored, key = _read("PULSE_SITE_PASSWORD_FILE"), _read("PULSE_SITE_SECRET_FILE")
    if not stored or not key:
        return None
    try:
        return stored, bytes.fromhex(key)
    except ValueError:
        return None


# ---------------------------------------------------------------- session cookie

def _sign(key: bytes, stored: str, body: str) -> str:
    bound = hashlib.sha256(stored.encode("utf-8")).hexdigest()
    return hmac.new(key, f"{body}|{bound}".encode("utf-8"), hashlib.sha256).hexdigest()


def issue(key: bytes, stored: str, *, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + SESSION_SECONDS)
    body = f"v1.{exp}.{secrets.token_hex(8)}"
    return f"{body}.{_sign(key, stored, body)}"


def valid(token: str | None, key: bytes, stored: str, *, now: float | None = None) -> bool:
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return False
    body, sig = ".".join(parts[:3]), parts[3]
    if len(sig) != 64 or any(c not in "0123456789abcdef" for c in sig):
        return False
    if not hmac.compare_digest(sig.encode("ascii"), _sign(key, stored, body).encode("ascii")):
        return False
    try:
        return int(parts[1]) > (now if now is not None else time.time())
    except ValueError:
        return False


# ---------------------------------------------------------------- failed-login limit

class Limiter:
    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for k in list(self._fails):
            kept = [t for t in self._fails[k] if now - t < LOCK_WINDOW]
            if kept:
                self._fails[k] = kept
            else:
                del self._fails[k]

    def locked(self, client: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            self._prune(now)
            total = sum(len(v) for v in self._fails.values())
            return len(self._fails.get(client, [])) >= LOCK_PER_CLIENT or total >= LOCK_TOTAL

    def fail(self, client: str, now: float | None = None) -> None:
        with self._lock:
            self._fails.setdefault(client, []).append(now if now is not None else time.time())

    def clear(self, client: str) -> None:
        with self._lock:
            self._fails.pop(client, None)


LIMITER = Limiter()


# ---------------------------------------------------------------- request helpers

def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def proxied(request: Request) -> bool:
    return "x-forwarded-for" in request.headers or "forwarded" in request.headers


def gated(request: Request) -> bool:
    if proxied(request):
        return True
    return os.environ.get("PULSE_SITE_AUTH") == "on" and not _is_loopback(request.client.host if request.client else None)


def client_id(request: Request) -> str:
    """The address the proxy saw (rightmost X-Forwarded-For entry, which our proxy appended), else the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def safe_next(value: str | None) -> str:
    """Only a same-site path: starts with one '/', no scheme, no backslash."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or "\n" in value or "\r" in value:
        return "/"
    return value


def _wants_json(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in JSON_PREFIXES)


# ---------------------------------------------------------------- pages

_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>登录 · Pulse</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#6b7280;--line:#d7dbe2;--accent:#2563eb;--err:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111418;--card:#1a1f26;--fg:#e5e7eb;--muted:#9ca3af;--line:#2d343e;--accent:#60a5fa;--err:#f87171}}}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:16px}}
form{{width:100%;max-width:340px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:28px 24px}}
h1{{font-size:18px;margin:0 0 4px}}p{{margin:0 0 20px;color:var(--muted);font-size:13px}}
label{{display:block;font-size:13px;margin-bottom:6px}}
input{{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--fg);font-size:15px}}
input:focus{{outline:2px solid var(--accent);outline-offset:1px}}
button{{margin-top:16px;width:100%;padding:10px;border:0;border-radius:6px;background:var(--accent);color:#fff;font-size:15px;cursor:pointer}}
.err{{color:var(--err);font-size:13px;margin:12px 0 0}}
</style></head><body>
<form method="post" action="/login">
<h1>Pulse 选股工作台</h1><p>请输入访问密码</p>
<label for="pw">密码</label>
<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
<input type="hidden" name="next" value="{next}">
<button type="submit">登录</button>
{error}
</form></body></html>"""


def login_page(next_path: str, error: str | None = None, status: int = 200) -> HTMLResponse:
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    resp = HTMLResponse(_PAGE.format(next=html.escape(safe_next(next_path), quote=True), error=err), status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _unauthorized(request: Request, message: str = "需要登录", status: int = 401) -> Response:
    path = request.url.path
    if _wants_json(path) or request.method not in ("GET", "HEAD"):
        return JSONResponse({"error": {"code": "SITE_LOGIN_REQUIRED", "message": message}}, status_code=status)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)


async def _bounded_body(request: Request) -> bytes | None:
    """The request body, or None once it exceeds MAX_BODY (read in chunks, so a huge or chunked body is never buffered)."""
    try:
        if int(request.headers.get("content-length", "0")) > MAX_BODY:
            return None
    except ValueError:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY:
            return None
    return bytes(buf)


async def _login_post(request: Request, creds: tuple[str, bytes]) -> Response:
    stored, key = creds
    who = client_id(request)
    raw = await _bounded_body(request)
    if raw is None:
        return JSONResponse({"error": {"code": "PAYLOAD_TOO_LARGE", "message": "请求过大"}}, status_code=413)
    form = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    nxt = safe_next((form.get("next") or ["/"])[0])
    if LIMITER.locked(who):
        return login_page(nxt, "尝试次数过多，请 15 分钟后再试", status=429)
    password = (form.get("password") or [""])[0]
    LIMITER.fail(who)                      # counted before the slow check, so concurrent attempts cannot pass the limit
    if not await asyncio.to_thread(verify_password, password, stored):
        return login_page(nxt, "密码不正确", status=401)
    LIMITER.clear(who)
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, issue(key, stored), max_age=SESSION_SECONDS, httponly=True, secure=_https(request),
                    samesite="lax", path="/")
    return resp


async def gate(request: Request, call_next):
    """HTTP middleware: pass ungated requests, serve /login and /logout, require a valid session for the rest."""
    if not gated(request):
        return await call_next(request)
    creds = credentials()
    path = request.url.path
    if creds is None:
        return JSONResponse({"error": {"code": "SITE_LOGIN_NOT_CONFIGURED", "message": "网站登录未配置"}}, status_code=503)
    stored, key = creds
    if path == "/logout":
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp
    if path == "/login":
        if request.method == "POST":
            return await _login_post(request, creds)
        if valid(request.cookies.get(COOKIE), key, stored):
            return RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
        return login_page(request.query_params.get("next") or "/")
    if not valid(request.cookies.get(COOKIE), key, stored):
        return _unauthorized(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
        return JSONResponse({"error": {"code": "CROSS_ORIGIN", "message": "跨站请求被拒绝"}}, status_code=403)
    return await call_next(request)


def same_origin(request: Request) -> bool:
    """A state-changing request must not come from another site: Origin (when sent) must name this host,
    and Sec-Fetch-Site (when sent) must be same-origin or none. Non-browser clients send neither and pass."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return origin.split("://", 1)[-1].rstrip("/") == host
