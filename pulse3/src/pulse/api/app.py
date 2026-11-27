"""FastAPI application: the /v1 operations of the one OpenAPI contract, plus the built web app.

Run: uv run uvicorn pulse.api.app:app --host 127.0.0.1 --port 8200
Routers only translate HTTP <-> application services; no ranking, plan or
ledger logic lives here (FINAL_SYSTEM_SPEC §2, §11).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pulse.api import deps
from pulse.api.envelope import ApiError, error_response

log = logging.getLogger("pulse.api")
WEB_DIST = Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI):
    from pulse.application.bootstrap import seed_jobs, seed_registry

    with deps.pool().connection() as conn:
        seed_registry(conn)
        seed_jobs(conn)
    yield
    deps.close_pool()


def create_app() -> FastAPI:
    from pulse.api.auth import validate_dev_principal

    validate_dev_principal()                      # refuse to start with an unguarded dev principal
    app = FastAPI(title="Pulse v3", version="3.0.0", lifespan=lifespan, docs_url="/v1/docs", openapi_url=None)

    from pulse.api.site_auth import gate

    app.middleware("http")(gate)                  # login gate for proxied (public) requests; loopback requests pass

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_: Request, exc: RequestValidationError):
        fe = [{"path": "/" + "/".join(map(str, e.get("loc", []))), "code": e.get("type", "invalid"),
               "message": e.get("msg", "invalid")} for e in exc.errors()]
        return error_response(ApiError(400, "BAD_REQUEST", "请求参数无效", field_errors=fe))

    from pulse.api import routes_user

    app.include_router(routes_user.router)
    for name in ("routes_market", "routes_account", "routes_decision", "routes_ops", "routes_research",
                 "routes_ext_a", "routes_ext_b", "routes_ext_c"):
        try:
            mod = __import__(f"pulse.api.{name}", fromlist=["router"])
        except ModuleNotFoundError:
            continue
        app.include_router(mod.router)

    import pkgutil

    import pulse.api as _api_pkg

    for info in sorted(pkgutil.iter_modules(_api_pkg.__path__), key=lambda i: i.name):
        if info.name.startswith("routes_bf_"):                # legacy-feature backfill lanes (docs/LEGACY_TRIAGE.md)
            app.include_router(__import__(f"pulse.api.{info.name}", fromlist=["router"]).router)

    from pulse.api import routes_market

    app.include_router(routes_market.config_router)  # /app-config.json, before the SPA catch-all

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """Process + database probe for operators (not a product operation of the contract)."""
        from fastapi.responses import JSONResponse

        try:
            with deps.pool().connection(timeout=5) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:  # noqa: BLE001 - the probe reports, it never raises
            return JSONResponse({"status": "degraded", "database": False, "error": type(exc).__name__}, status_code=503)
        return {"status": "ok", "database": True}

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path == "v1" or path.startswith("v1/"):        # an unknown API path is a JSON 404, never the web page
                return error_response(ApiError(404, "NOT_FOUND", "接口不存在"))
            target = WEB_DIST / path
            if path and target.is_file() and WEB_DIST in target.resolve().parents:
                return FileResponse(target)
            return FileResponse(WEB_DIST / "index.html")

    return app


app = create_app()
