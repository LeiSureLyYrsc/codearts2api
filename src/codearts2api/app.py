"""FastAPI application: OpenAI-compatible gateway plus check-in admin APIs."""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from codearts2api import __version__
from codearts2api.auth import get_settings, require_api_key, require_upstream_credentials
from codearts2api.chat import (
    format_sse_done,
    format_sse_json,
    normalize_chunk,
    normalize_completion,
    openai_error,
)
from codearts2api.checkin import CheckinResult, run_checkin
from codearts2api.client import SnapAccessClient, UpstreamError
from codearts2api.config import Settings
from codearts2api.credentials import CredentialResolver
from codearts2api.logging_setup import configure_logging
from codearts2api.oauth.flow import CallbackServer, LoginError, build_authorize_url
from codearts2api.oauth.dpop import generate_p256_keypair
from codearts2api.oauth.pkce import generate_pkce_pair, generate_ticket_id
from codearts2api.models_catalog import filter_and_map_models, strip_codearts_prefix
from codearts2api.scheduler import CheckinScheduler
from codearts2api.schemas import ChatCompletionsRequest, CheckinRequest

logger = logging.getLogger(__name__)

protected = APIRouter(dependencies=[Depends(require_api_key)])


class ModelCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = ttl_seconds
        self._expires_at = 0.0
        self._items: list[dict[str, Any]] | None = None

    def get(self) -> list[dict[str, Any]] | None:
        if self._items is None or time.monotonic() >= self._expires_at:
            return None
        return self._items

    def set(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self._expires_at = time.monotonic() + self.ttl

    def clear(self) -> None:
        self._items = None
        self._expires_at = 0.0


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http: httpx.AsyncClient | None = None
        self.client: SnapAccessClient | None = None
        self.resolver = CredentialResolver(settings)
        self.scheduler: CheckinScheduler | None = None
        self.model_cache = ModelCache(settings.model_cache_ttl)
        self.last_checkin: dict[str, Any] | None = None
        self.last_checkin_at: str | None = None
        self.login_server: CallbackServer | None = None
        self.login_authorize_url: str | None = None

    def snap(self) -> SnapAccessClient:
        if self.client is None:
            raise HTTPException(status_code=503, detail="HTTP client is not ready")
        return self.client


def _state(request: Request) -> AppState:
    return request.app.state.runtime


async def _execute_checkin(runtime: AppState, all_claimable: bool | None = None) -> CheckinResult:
    claim_all = runtime.settings.checkin_claim_all if all_claimable is None else all_claimable
    result = await run_checkin(
        runtime.snap(),
        campaign_type=runtime.settings.campaign_type,
        all_claimable=claim_all,
    )
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(runtime.settings.timezone)
    runtime.last_checkin = result.to_dict()
    runtime.last_checkin_at = datetime.now(tz).isoformat()
    logger.info(
        "check-in finished status=%s account_type=%s",
        result.status,
        result.account_type,
    )
    return result


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.secret_values_for_redaction())
    runtime = AppState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(settings.http_timeout)
        runtime.http = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        runtime.client = SnapAccessClient(settings, runtime.http, runtime.resolver)
        app.state.runtime = runtime
        app.state.settings = settings

        if settings.scheduler_enabled:
            runtime.scheduler = CheckinScheduler(
                cron=settings.checkin_cron,
                timezone=settings.timezone,
                job=lambda: _execute_checkin(runtime),
            )
            runtime.scheduler.start()
            if settings.checkin_run_on_startup and runtime.resolver.has_any():
                try:
                    await _execute_checkin(runtime)
                except Exception:  # noqa: BLE001
                    logger.exception("Startup check-in failed; scheduler will retry on schedule")
            elif settings.checkin_run_on_startup:
                logger.warning(
                    "Startup check-in skipped: no OAuth session or CODEARTS_AK/SK. "
                    "Run `uv run codearts2api login` or set IAM keys."
                )

        try:
            yield
        finally:
            if runtime.login_server is not None:
                runtime.login_server.stop()
                runtime.login_server = None
            if runtime.scheduler is not None:
                runtime.scheduler.shutdown()
            if runtime.http is not None:
                await runtime.http.aclose()

    app = FastAPI(
        title="codearts2api",
        version=__version__,
        description="Huawei Cloud China 码道 CodeArts Agent to OpenAI-compatible API gateway",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.runtime = runtime

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, Any]:
        runtime = request.app.state.runtime
        ready = runtime.resolver.has_any() and settings.has_api_key()
        return {
            "status": "ok" if ready else "degraded",
            "credentials_configured": runtime.resolver.has_any(),
            "api_key_configured": settings.has_api_key(),
            "credential_status": runtime.resolver.status(),
        }

    @protected.get("/v1/models")
    async def list_models(
        request: Request,
        refresh: bool = Query(default=False),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        if not _state(request).resolver.has_any():
            require_upstream_credentials(settings)
        runtime = _state(request)
        if not refresh:
            cached = runtime.model_cache.get()
            if cached is not None:
                return {"object": "list", "data": cached, "cached": True}
        try:
            payload = await runtime.snap().list_builtin_models()
        except UpstreamError as exc:
            raise HTTPException(status_code=exc.status_code, detail=openai_error(str(exc), exc.status_code)["error"]) from exc
        items = filter_and_map_models(payload, created=int(time.time()))
        runtime.model_cache.set(items)
        return {"object": "list", "data": items, "cached": False}

    @protected.post("/v1/chat/completions")
    async def chat_completions(
        body: ChatCompletionsRequest,
        request: Request,
        settings: Settings = Depends(get_settings),
    ):
        if not _state(request).resolver.has_any():
            require_upstream_credentials(settings)
        runtime = _state(request)
        model = strip_codearts_prefix(body.model)
        upstream_payload = body.model_dump(exclude_none=True)
        upstream_payload["model"] = model

        if body.stream:
            return StreamingResponse(
                _stream_chat(runtime, upstream_payload, model),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        try:
            raw = await runtime.snap().post_chat(upstream_payload)
        except UpstreamError as exc:
            return JSONResponse(
                status_code=exc.status_code if exc.status_code >= 400 else 502,
                content=openai_error(str(exc), exc.status_code),
            )
        return normalize_completion(raw, model)

    @protected.post("/admin/checkin")
    async def post_checkin(
        request: Request,
        body: CheckinRequest | None = None,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        if not _state(request).resolver.has_any():
            require_upstream_credentials(settings)
        runtime = _state(request)
        all_claimable = body.all_claimable if body is not None else False
        result = await _execute_checkin(runtime, all_claimable=all_claimable)
        return {
            "result": result.to_dict(),
            "checked_in_at": runtime.last_checkin_at,
        }

    @protected.get("/admin/checkin")
    @protected.get("/admin/status")
    async def get_checkin_status(request: Request) -> dict[str, Any]:
        runtime = _state(request)
        scheduler = runtime.scheduler.snapshot() if runtime.scheduler else {
            "enabled": False,
            "cron": runtime.settings.checkin_cron,
            "timezone": runtime.settings.timezone,
            "last_run_at": None,
            "next_run_at": None,
            "last_error": None,
        }
        account_type = None
        is_credit = None
        if runtime.last_checkin:
            account_type = runtime.last_checkin.get("account_type")
            is_credit = runtime.last_checkin.get("is_credit_package")
        return {
            "last_checkin_at": runtime.last_checkin_at,
            "last_checkin": runtime.last_checkin,
            "account_type": account_type,
            "is_credit_package": is_credit,
            "scheduler": scheduler,
            "campaign_type": runtime.settings.campaign_type,
            "claim_all_default": runtime.settings.checkin_claim_all,
            "credentials": runtime.resolver.status(),
        }

    @protected.get("/admin/login")
    async def get_admin_login(
        request: Request,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        """Start (or resume) the official CLI loopback OAuth flow and return the authorize URL."""
        runtime = _state(request)
        if runtime.login_server is not None:
            return {
                "authorize_url": runtime.login_authorize_url,
                "callback_url": runtime.login_server.callback_url,
                "status": "pending",
                "message": "A login is already waiting on the local callback. Open the authorize_url in a browser.",
            }
        endpoints = runtime.resolver.endpoints()
        pkce = generate_pkce_pair()
        ticket_id = generate_ticket_id()
        dpop = generate_p256_keypair()
        server = CallbackServer(endpoints, port=settings.oauth_callback_port)
        server.start(pkce, ticket_id, dpop)
        authorize_url = build_authorize_url(
            endpoints,
            port=server.port,
            code_challenge=pkce.code_challenge,
            ticket_id=ticket_id,
            code_challenge_method=pkce.code_challenge_method,
        )
        runtime.login_server = server
        runtime.login_authorize_url = authorize_url
        return {
            "authorize_url": authorize_url,
            "callback_url": server.callback_url,
            "status": "pending",
            "client_id": endpoints.client_id,
            "message": (
                "Open authorize_url in a browser, log in to Huawei Cloud, and confirm authorization. "
                "The official client callback is http://127.0.0.1:<port>/oauth/callback. "
                "If this process is not on the same machine as the browser, set up a local port forward, "
                "or run `uv run codearts2api login` on the machine that has the browser. "
                "Then call GET /admin/login/status."
            ),
        }

    @protected.get("/admin/login/status")
    async def get_admin_login_status(request: Request) -> dict[str, Any]:
        runtime = _state(request)
        server = runtime.login_server
        if server is None:
            return {"status": "idle", "session": runtime.resolver.status()}
        if server._done.is_set():  # noqa: SLF001 — poll the in-process login
            try:
                session = server.wait(timeout=0.1)
                runtime.resolver.store.save(session)
            except LoginError as exc:
                return {"status": "error", "message": str(exc)}
            finally:
                server.stop()
                runtime.login_server = None
                runtime.login_authorize_url = None
            return {"status": "logged_in", "session": runtime.resolver.status()}
        return {
            "status": "pending",
            "authorize_url": runtime.login_authorize_url,
            "callback_url": server.callback_url,
        }

    @protected.post("/admin/benefit-claim")
    async def post_benefit_claim(
        request: Request,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        """Optional, separate from daily check-in. Requires OPENGW_BASE_URL."""
        if not _state(request).resolver.has_any():
            require_upstream_credentials(settings)
        return await _state(request).snap().claim_opengw_benefit()

    app.include_router(protected)
    return app


async def _stream_chat(runtime: AppState, payload: dict[str, Any], model: str):
    client = runtime.snap()
    try:
        async with client.stream_chat(payload) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                try:
                    parsed = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    parsed = raw.decode("utf-8", errors="replace")
                message = str(parsed) if not isinstance(parsed, dict) else (
                    parsed.get("message")
                    or (parsed.get("error") or {}).get("message")
                    or f"Upstream stream failed with HTTP {response.status_code}"
                )
                yield format_sse_json(openai_error(str(message), response.status_code))
                yield format_sse_done()
                return

            buffer = ""
            saw_done = False
            async for text in response.aiter_text():
                buffer += text
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.rstrip("\r")
                    if line == "":
                        continue
                    if line.startswith(":"):
                        # SSE comment / keepalive
                        continue
                    data = line
                    if line.startswith("data:"):
                        data = line[5:].lstrip()
                    elif line.startswith("event:") or line.startswith("id:") or line.startswith("retry:"):
                        continue
                    if data.strip() == "[DONE]":
                        saw_done = True
                        yield format_sse_done()
                        continue
                    try:
                        parsed = json.loads(data)
                    except ValueError:
                        # Not JSON; forward the data line as-is rather than inventing a schema.
                        yield f"data: {data}\n\n"
                        continue
                    yield format_sse_json(normalize_chunk(parsed, model))
            leftover = buffer.strip()
            if leftover:
                if leftover.startswith("data:"):
                    leftover = leftover[5:].lstrip()
                if leftover == "[DONE]":
                    saw_done = True
                    yield format_sse_done()
                else:
                    try:
                        parsed = json.loads(leftover)
                        yield format_sse_json(normalize_chunk(parsed, model))
                    except ValueError:
                        yield f"data: {leftover}\n\n"
            if not saw_done:
                yield format_sse_done()
    except UpstreamError as exc:
        yield format_sse_json(openai_error(str(exc), exc.status_code))
        yield format_sse_done()
    except httpx.HTTPError as exc:
        logger.exception("Streaming chat failed")
        yield format_sse_json(openai_error(f"Upstream stream error: {exc}", 502))
        yield format_sse_done()


# Default app instance for ``uvicorn codearts2api.app:app`` / ``python -m codearts2api``.
app = create_app()
