from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import STATIC_DIR, AppConfig, credentials_status, get_env_config, mask_secret, write_env_config
from app.runtime import runtime
from app.store import append_event, events_since, load_config, save_config


app = FastAPI(title="Deribit DDH Workbench")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(Exception)
async def json_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    append_event("error", "server_error", {"path": request.url.path, "message": str(exc)})
    return JSONResponse(status_code=500, content={"detail": str(exc) or "Internal Server Error"})


class ApiCredentialsRequest(BaseModel):
    mode: Literal["testnet", "mainnet"] = "testnet"
    client_id: str = ""
    client_secret: str = ""


@app.middleware("http")
async def no_store_assets(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def index_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "Deribit DDH Workbench"})


@app.get("/api/config")
async def get_config() -> JSONResponse:
    config = load_config()
    return JSONResponse({"config": config.model_dump(), "credentials": credentials_status()})


@app.post("/api/config")
async def update_config(payload: AppConfig) -> JSONResponse:
    old_config = load_config()
    save_config(payload)
    cancelled = []
    cancel_error = ""
    try:
        cancelled = await runtime.cancel_orders_for_config_change(old_config, payload)
    except Exception as exc:
        cancel_error = str(exc)
        append_event("error", "config_change_cancel_failed", {"message": cancel_error})
    append_event("info", "config_saved", {"mode": payload.mode, "dry_run": payload.dry_run})
    return JSONResponse(
        {
            "ok": True,
            "config": payload.model_dump(),
            "cancelled_orders": cancelled,
            "cancel_error": cancel_error,
        }
    )


@app.post("/api/credentials")
async def update_credentials(payload: ApiCredentialsRequest) -> JSONResponse:
    current = get_env_config(payload.mode)
    client_id = payload.client_id.strip() or current.deribit_client_id
    client_secret = payload.client_secret.strip() or current.deribit_client_secret
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail=f"请填写 {payload.mode} API ID 和 API Secret。")
    try:
        env = write_env_config(payload.mode, client_id, client_secret)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_event(
        "info",
        "api_credentials_saved",
        {"mode": payload.mode, "client_id_masked": mask_secret(env.deribit_client_id)},
    )
    return JSONResponse({"ok": True, "credentials": credentials_status()})


@app.get("/api/status")
async def get_status() -> JSONResponse:
    return JSONResponse(runtime.status())


@app.get("/api/open-orders")
async def get_open_orders() -> JSONResponse:
    try:
        return JSONResponse({"open_orders": await runtime.open_ddh_orders()})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/fill-events")
async def get_fill_events() -> JSONResponse:
    events = [item for item in events_since(days=7) if item.get("event") in {"order_submitted", "order_filled"}]
    return JSONResponse({"events": events, "days": 7})


@app.post("/api/runtime/start")
async def start_runtime() -> JSONResponse:
    return JSONResponse(await runtime.start())


@app.post("/api/runtime/stop")
async def stop_runtime() -> JSONResponse:
    return JSONResponse(await runtime.stop())


@app.post("/api/runtime/preview")
async def preview() -> JSONResponse:
    try:
        return JSONResponse(await runtime.run_once(reason="manual_preview", force=True, execute=False))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runtime/execute")
async def execute() -> JSONResponse:
    try:
        return JSONResponse(await runtime.run_once(reason="manual", force=True, execute=True))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runtime/execute/{currency}")
async def execute_currency(currency: str) -> JSONResponse:
    try:
        return JSONResponse(await runtime.run_once(reason="immediate", force=True, execute=True, currencies=[currency]))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
