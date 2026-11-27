"""Site login gate: proxied requests need a session, loopback requests pass, the gate fails closed, failed logins lock."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pulse.api import site_auth as SA
from pulse.workers import site_password as SP

PW = "correct horse battery"
XFF = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}


@pytest.fixture(autouse=True)
def _cheap_scrypt(monkeypatch):
    monkeypatch.setattr(SA, "SCRYPT_N", 2 ** 10)          # fast tests; the stored hash carries its own n
    SA.LIMITER._fails.clear()
    yield
    SA.LIMITER._fails.clear()


@pytest.fixture()
def creds(tmp_path, monkeypatch):
    pw, key = tmp_path / "site_password.txt", tmp_path / "site_secret.txt"
    pw.write_text(SA.hash_password(PW), encoding="utf-8")
    key.write_text("ab" * 32, encoding="utf-8")
    monkeypatch.setenv("PULSE_SITE_PASSWORD_FILE", str(pw))
    monkeypatch.setenv("PULSE_SITE_SECRET_FILE", str(key))
    monkeypatch.delenv("PULSE_SITE_AUTH", raising=False)
    return pw, key


def _app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(SA.gate)

    @app.get("/v1/ping")
    def ping():
        return {"ok": True}

    @app.get("/{path:path}")
    def page(path: str):
        return {"page": path}

    return app


@pytest.fixture()
def client():
    with TestClient(_app(), base_url="http://testserver") as c:
        yield c


def _login(c, password=PW, nxt="/", headers=XFF):
    return c.post("/login", data={"password": password, "next": nxt}, headers=headers, follow_redirects=False)


def test_direct_requests_pass_without_a_login(client, creds):
    assert client.get("/v1/ping").json() == {"ok": True}


def test_proxied_requests_need_a_login_page_redirect_and_json_401(client, creds):
    r = client.get("/board?x=1", headers=XFF, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/board%3Fx%3D1"
    r = client.get("/v1/ping", headers=XFF)
    assert r.status_code == 401 and r.json()["error"]["code"] == "SITE_LOGIN_REQUIRED"
    page = client.get("/login?next=/board", headers=XFF)
    assert page.status_code == 200 and 'type="password"' in page.text and 'value="/board"' in page.text


def test_a_good_password_sets_a_secure_httponly_cookie_and_opens_the_site(client, creds):
    r = _login(client, nxt="/board")
    assert r.status_code == 303 and r.headers["location"] == "/board"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    client.cookies.set(SA.COOKIE, r.cookies[SA.COOKIE])
    assert client.get("/v1/ping", headers=XFF).json() == {"ok": True}
    out = client.get("/logout", headers=XFF, follow_redirects=False)
    assert out.status_code == 303 and SA.COOKIE in out.headers["set-cookie"]


def test_a_wrong_password_is_refused_and_the_next_target_stays_on_site(client, creds):
    r = _login(client, password="wrong password!!", nxt="https://evil.example/")
    assert r.status_code == 401 and "密码不正确" in r.text and 'value="/"' in r.text
    assert SA.safe_next("//evil.example") == "/" and SA.safe_next("/\\evil") == "/" and SA.safe_next("/ok?a=1") == "/ok?a=1"


def test_five_failures_lock_the_client_even_with_the_right_password(client, creds):
    for _ in range(SA.LOCK_PER_CLIENT):
        assert _login(client, password="nope nope nope").status_code == 401
    assert _login(client).status_code == 429
    other = {**XFF, "X-Forwarded-For": "198.51.100.7"}
    assert _login(client, headers=other).status_code == 303                  # another client is not locked


def test_the_gate_fails_closed_without_a_password(client, creds, monkeypatch):
    monkeypatch.delenv("PULSE_SITE_PASSWORD_FILE")
    assert client.get("/board", headers=XFF).status_code == 503
    assert client.get("/v1/ping").status_code == 200                         # loopback still works


def test_a_tampered_expired_or_old_password_cookie_is_refused(creds):
    pw, _ = creds
    stored, key = SA.credentials()
    tok = SA.issue(key, stored, now=1000.0)
    assert SA.valid(tok, key, stored, now=1001.0)
    assert not SA.valid(tok, key, stored, now=1000.0 + SA.SESSION_SECONDS + 1)
    assert not SA.valid(tok[:-1] + ("0" if tok[-1] != "0" else "1"), key, stored, now=1001.0)
    assert not SA.valid(tok, key, SA.hash_password("another password"), now=1001.0)   # a new password logs everyone out


def test_odd_cookies_big_bodies_and_cross_site_posts_are_refused_without_a_500(client, creds):
    stored, key = SA.credentials()
    assert not SA.valid("v1.9999999999.aa.é", key, stored)                # used to raise TypeError (a 500)
    assert client.get("/v1/ping", headers={**XFF, "Cookie": f"{SA.COOKIE}=v1.9999999999.aa.zz"}).status_code == 401
    big = client.post("/login", content=b"password=" + b"x" * (SA.MAX_BODY + 1),
                      headers={**XFF, "Content-Type": "application/x-www-form-urlencoded"})
    assert big.status_code == 413
    ok = _login(client)
    client.cookies.set(SA.COOKIE, ok.cookies[SA.COOKIE])
    assert client.post("/v1/ping", headers={**XFF, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/v1/ping", headers={**XFF, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/v1/ping", headers={**XFF, "Origin": "http://testserver"}).status_code == 405   # same origin: reaches the app


def test_non_loopback_clients_are_gated_when_site_auth_is_on(client, creds, monkeypatch):
    monkeypatch.setenv("PULSE_SITE_AUTH", "on")
    assert client.get("/v1/ping").status_code == 401                         # the test client is not a loopback address


def test_the_password_tool_writes_a_hash_and_a_key_and_refuses_short_or_mismatched(tmp_path, monkeypatch):
    pw, key = tmp_path / "p.txt", tmp_path / "k.txt"
    monkeypatch.setenv("PULSE_SITE_PASSWORD_FILE", str(pw))
    monkeypatch.setenv("PULSE_SITE_SECRET_FILE", str(key))
    def answers(*xs):
        it = iter(xs)
        return lambda _p: next(it)

    assert SP.run(prompt=answers("short", "short")) == 2 and not pw.exists()
    assert SP.run(prompt=answers(PW, PW + "x")) == 2 and not pw.exists()
    assert SP.run(prompt=lambda _p: PW) == 0
    stored = pw.read_text(encoding="utf-8").strip()
    assert PW not in stored and SA.verify_password(PW, stored) and len(key.read_text().strip()) == 64


def test_create_app_installs_the_gate():
    import inspect

    from pulse.api import app as A
    assert "app.middleware(\"http\")(gate)" in inspect.getsource(A.create_app)
