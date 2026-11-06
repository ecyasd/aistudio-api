"""FastAPI application entrypoint."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aistudio_api.application.api_service_gemini import classify_gemini_error_payload
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_configured_logging_settings,
)
from aistudio_api.infrastructure.utils import telemetry
from aistudio_api.infrastructure.utils.logger import (
    get_logger,
    setup_logging,
)

from .dependencies import require_api_key, require_web_auth
from .routes_accounts import router as accounts_router
from .routes_gemini import router as gemini_router
from .routes_models import router as models_router
from .routes_observability import router as observability_router
from .routes_openai import router as openai_router
from .routes_system import (
    protected_router as system_protected_router,
    public_router as system_public_router,
)
from .state import runtime_state

_log_cfg = get_configured_logging_settings()
setup_logging(str(_log_cfg.get("level", "INFO")))
logger = get_logger("server")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from aistudio_api.infrastructure.waa.daemon import (
        ensure_signer_ready,
        stop_signer_daemon,
    )

    # 启动时自动检查并拉起本地 WAA 签名服务（若配置为本地节点）
    ensure_signer_ready()

    from aistudio_api.application.account_rotator import init_rotator
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.infrastructure.account.account_store import AccountStore

    client = AIStudioClient()
    runtime_state.client = client

    # 初始化账号管理服务
    account_store = AccountStore()
    account_service = AccountService(account_store)
    runtime_state.account_service = account_service

    # 初始化黏性账号调度器
    rotator = init_rotator(account_store)
    runtime_state.rotator = rotator

    logger.info(
        "服务初始化完成: 监听端口=%d, 已加载账号数=%d",
        settings.port,
        len(account_store.list_accounts()),
    )
    logger.debug(
        "账号初始状态: %s",
        [
            f"{a.id}(u/{a.auth_user},tier={a.tier},enabled={a.enabled})"
            for a in account_store.list_accounts()
        ],
    )

    telemetry.event("started")

    yield
    logger.info("服务正在关闭")
    telemetry.event("stopped")
    if client:
        try:
            await client.close()
        except Exception as e:
            logger.debug("关闭客户端失败: %s", e)
    stop_signer_daemon()
    runtime_state.client = None
    runtime_state.account_service = None
    runtime_state.rotator = None


app = FastAPI(title="AI Studio API", lifespan=lifespan)
app.include_router(system_public_router)
app.include_router(system_protected_router, dependencies=[Depends(require_web_auth)])
app.include_router(
    accounts_router, prefix="/api", dependencies=[Depends(require_web_auth)]
)
app.include_router(accounts_router, dependencies=[Depends(require_web_auth)])
app.include_router(observability_router, dependencies=[Depends(require_web_auth)])
app.include_router(openai_router, dependencies=[Depends(require_api_key)])
app.include_router(gemini_router, dependencies=[Depends(require_api_key)])
app.add_middleware(telemetry.ObservationMiddleware)
app.include_router(models_router, dependencies=[Depends(require_api_key)])


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    code, msg, status_str = classify_gemini_error_payload(exc)
    logger.warning("HTTP 请求失败: status=%d", code)
    if _request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=code,
            content={
                "error": {
                    "message": msg,
                    "type": {
                        401: "authentication_error",
                        403: "permission_error",
                        429: "rate_limit_error",
                    }.get(
                        code,
                        "invalid_request_error" if code < 500 else "upstream_error",
                    ),
                    "code": code,
                }
            },
            headers=getattr(exc, "headers", None),
        )
    return JSONResponse(
        status_code=code,
        content={
            "error": {
                "code": code,
                "message": msg,
                "status": status_str,
            }
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    code, msg, status_str = 400, "Invalid request parameters", "INVALID_ARGUMENT"
    logger.warning("请求参数校验失败")
    if _request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=code,
            content={
                "error": {
                    "message": msg,
                    "type": "invalid_request_error" if code < 500 else "upstream_error",
                    "code": code,
                }
            },
            headers=getattr(exc, "headers", None),
        )
    return JSONResponse(
        status_code=code,
        content={
            "error": {
                "code": code,
                "message": msg,
                "status": status_str,
            }
        },
    )


# 挂载前端静态资源与 SPA 路由支持
static_dir = Path(__file__).resolve().parents[1] / "static"
index_html_path = static_dir / "index.html"

if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


@app.get("/auth/check")
async def auth_check():
    """检查认证状态，用于前端判断是否需要登录。"""

    return {"auth_enabled": True}


class SpaNavigationMiddleware:
    """Serve HTML navigation without wrapping the generation stream in a task group."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        accept = request.headers.get("accept", "")
        html_navigation = (
            request.method == "GET"
            and "text/html" in accept
            and "application/json" not in accept
        )
        api_path = path.startswith(
            (
                "/api",
                "/v1beta",
                "/v1/",
                "/auth",
                "/assets",
                "/static",
                "/health",
                "/stats",
                "/rotation",
                "/config",
            )
        )
        accounts_navigation = path in ("/accounts", "/accounts/")
        if (
            html_navigation
            and (accounts_navigation or not api_path)
            and index_html_path.is_file()
        ):
            response = FileResponse(
                index_html_path, headers={"Vary": "Accept", "Cache-Control": "no-store"}
            )
            await response(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start" and (
                api_path or path.startswith("/accounts")
            ):
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                if path.startswith(("/accounts", "/api/accounts")):
                    headers.add_vary_header("Accept")
            await send(message)

        await self.app(scope, receive, with_headers)


app.add_middleware(SpaNavigationMiddleware)


@app.get("/")
async def root_index():
    """根路径兜底响应。"""
    if index_html_path.is_file():
        response = FileResponse(index_html_path)
        response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        return response
    return JSONResponse({"message": "AI Studio API Web UI"})


def main():
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio API Server")
    parser.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args()

    import uvicorn

    logger.info("启动服务: 监听端口=%d", args.port)
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
