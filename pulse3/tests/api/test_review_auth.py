"""Review fixes H2/H3: sessionAuth is enforced on every operation that declares it; the dev principal is loopback+dev only."""
import uuid

import pytest
from fastapi.testclient import TestClient

PA, PB = uuid.UUID(int=101), uuid.UUID(int=102)


def _reset_auth():
    from pulse.api import auth
    auth._sessions.cache_clear()


@pytest.fixture()
def anon(pg_dsn, monkeypatch):
    """No dev principal: only cookie sessions authenticate."""
    monkeypatch.setenv("PULSE_DSN", pg_dsn)
    monkeypatch.delenv("PULSE_DEV_PRINCIPAL", raising=False)
    monkeypatch.setenv("PULSE_SESSIONS", f"tokA={PA},tokB={PB}")
    _reset_auth()
    from pulse.api import deps
    from pulse.api.app import create_app
    deps.close_pool()
    with TestClient(create_app()) as c:
        yield c
    deps.close_pool()
    _reset_auth()


# ---------------------------------------------------------------- H2
@pytest.mark.pg
def test_h2_get_model_run_requires_a_session(anon):
    r = anon.get(f"/v1/analysis-runs/{uuid.uuid4()}")
    assert r.status_code == 403 and r.json()["error"]["code"] == "AUTH_REQUIRED"
    anon.cookies.set("pulse_session", "tokA")
    assert anon.get(f"/v1/analysis-runs/{uuid.uuid4()}").status_code == 404           # authenticated: the run just does not exist


@pytest.mark.pg
def test_h2_get_job_run_requires_session_and_is_limited_to_its_initiator(anon):
    r = anon.get(f"/v1/jobs/{uuid.uuid4()}")
    assert r.status_code == 403 and r.json()["error"]["code"] == "AUTH_REQUIRED"
    body = {"instance_id": str(uuid.uuid4()), "hypothesis_id": str(uuid.uuid4()), "hypothesis_version": 1,
            "evaluation_context_id": str(uuid.uuid4()), "run_purpose": "screening", "random_seed": 1}
    anon.cookies.set("pulse_session", "tokA")
    made = anon.post("/v1/experiments", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert made.status_code == 202, made.text
    run_id = made.json()["data"]["job_run_id"]
    assert anon.get(f"/v1/jobs/{run_id}").status_code == 200                          # the initiator
    anon.cookies.set("pulse_session", "tokB")
    other = anon.get(f"/v1/jobs/{run_id}")
    assert other.status_code == 404 and other.json()["error"]["code"] == "NOT_FOUND"  # someone else's job is not disclosed


@pytest.mark.pg
def test_h2_job_without_recorded_initiator_is_readable_by_any_session(anon, pg_dsn):
    import psycopg
    from datetime import datetime, timezone
    from psycopg.rows import dict_row
    from pulse.adapters.postgres.jobs import PgJobStore
    from pulse.api.routes_research import RESEARCH_JOB
    run = uuid.uuid4()
    with psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True) as c:
        PgJobStore(c).enqueue(job_run_id=run, job_id=RESEARCH_JOB, job_version=1, idempotency_key=f"sys:{run}", parameter_hash="a" * 64,
                              scheduled_for=datetime.now(timezone.utc), resource_type="strategy_instance", resource_id=uuid.uuid4())
    anon.cookies.set("pulse_session", "tokB")
    assert anon.get(f"/v1/jobs/{run}").status_code == 200


# ---------------------------------------------------------------- H3
def _env(monkeypatch, **kw):
    for k in ("PULSE_DEV_PRINCIPAL", "PULSE_ENV", "PULSE_API_HOST"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_h3_dev_principal_without_dev_env_refuses_startup(monkeypatch):
    from pulse.api.app import create_app
    _env(monkeypatch, PULSE_DEV_PRINCIPAL=str(PA))
    with pytest.raises(RuntimeError, match="PULSE_DEV_PRINCIPAL"):
        create_app()
    _env(monkeypatch, PULSE_DEV_PRINCIPAL=str(PA), PULSE_ENV="prod")
    with pytest.raises(RuntimeError, match="PULSE_ENV"):
        create_app()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])
def test_h3_dev_principal_on_a_non_loopback_bind_refuses_startup(monkeypatch, host):
    from pulse.api.app import create_app
    _env(monkeypatch, PULSE_DEV_PRINCIPAL=str(PA), PULSE_ENV="dev", PULSE_API_HOST=host)
    with pytest.raises(RuntimeError, match="loopback"):
        create_app()


@pytest.mark.parametrize("host", [None, "127.0.0.1", "::1", "localhost"])
def test_h3_dev_principal_on_loopback_in_dev_is_allowed(monkeypatch, host):
    from pulse.api.app import create_app
    _env(monkeypatch, PULSE_DEV_PRINCIPAL=str(PA), PULSE_ENV="dev", **({} if host is None else {"PULSE_API_HOST": host}))
    create_app()


def test_h3_no_dev_principal_needs_no_dev_env(monkeypatch):
    from pulse.api.app import create_app
    _env(monkeypatch)
    create_app()


@pytest.mark.pg
def test_h3_invalid_cookie_is_403_and_never_falls_back_to_the_dev_principal(pg_dsn, monkeypatch):
    _env(monkeypatch, PULSE_DEV_PRINCIPAL=str(PA), PULSE_ENV="dev", PULSE_API_HOST="127.0.0.1", PULSE_DSN=pg_dsn,
         PULSE_SESSIONS=f"tokB={PB}")
    _reset_auth()
    from pulse.api import deps
    from pulse.api.app import create_app
    deps.close_pool()
    with TestClient(create_app()) as c:
        assert c.get("/v1/watchlists").status_code == 200                              # no cookie: dev principal (loopback dev only)
        c.cookies.set("pulse_session", "forged")
        r = c.get("/v1/watchlists")
        assert r.status_code == 403 and r.json()["error"]["code"] == "AUTH_REQUIRED"
        c.cookies.set("pulse_session", "tokB")
        assert c.get("/v1/watchlists").status_code == 200
    deps.close_pool()
    _reset_auth()


# ---------------------------------------------------------------- Low: authenticate before validating
@pytest.mark.pg
def test_low_create_experiment_authenticates_before_validating(anon):
    bad = {"nonsense": True}
    r = anon.post("/v1/experiments", json=bad, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 403 and r.json()["error"]["code"] == "AUTH_REQUIRED"          # not a 400 that describes the schema
    anon.cookies.set("pulse_session", "tokA")
    r = anon.post("/v1/experiments", json=bad, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CONTRACT_VIOLATION"
