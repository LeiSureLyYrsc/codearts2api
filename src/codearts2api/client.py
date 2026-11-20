"""Signed snap-access HTTP client (async httpx)."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from codearts2api.config import Settings
from codearts2api.credentials import CredentialResolver, ResolvedCredential
from codearts2api.signing import sign_request

logger = logging.getLogger(__name__)

PLUGIN_STATISTICS_PATH = "/snap-manager/v1/statistics/plugin"
MODELS_PATH = "/v1/model/builtin"
CHAT_PATH = "/api/v2/chat/completions"
DELIVERY_PATH = "/v1/ops/delivery"
CLAIM_PATH = "/v1/ops/claim"
CONFIRM_PATH = "/v1/ops/confirm"
BENEFIT_CLAIM_PATH = "/api/v1/benefit/claim"


def json_body(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class UpstreamError(Exception):
    def __init__(self, message: str, status_code: int, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class SnapAccessClient:
    """Calls the official-client snap-access gateway with SDK-HMAC-SHA256."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        resolver: CredentialResolver | None = None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._resolver = resolver or CredentialResolver(settings)

    def _credential(self) -> ResolvedCredential:
        try:
            return self._resolver.resolve()
        except RuntimeError as exc:
            raise UpstreamError(str(exc), 503) from exc

    def _ak_sk(self) -> tuple[str, str]:
        cred = self._credential()
        return cred.access_key_id, cred.secret_access_key

    def _token(self) -> str | None:
        return self._credential().security_token

    def _unsigned_headers(self) -> dict[str, str] | None:
        language = self._settings.x_language
        if language:
            return {"X-Language": language}
        return None

    def _build_url(self, path: str, query: dict[str, str] | None = None) -> str:
        base = self._settings.snap_base_url.rstrip("/") + "/"
        url = urljoin(base, path.lstrip("/"))
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        return url

    def sign(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        ak, sk = self._ak_sk()
        outgoing = dict(headers or {})
        if body and "Content-Type" not in {k.title() for k in outgoing} and not any(
            k.lower() == "content-type" for k in outgoing
        ):
            outgoing["Content-Type"] = "application/json"
        signed = sign_request(
            method=method,
            url=url,
            ak=ak,
            sk=sk,
            headers=outgoing,
            body=body,
            security_token=self._token(),
            project_id=self._settings.codearts_project_id,
            extra_unsigned_headers=self._unsigned_headers(),
        )
        return signed.headers

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        payload: Any = None,
        body: bytes | None = None,
        expected: tuple[int, ...] = (200, 201),
    ) -> tuple[Any, int]:
        if body is None and payload is not None:
            body = json_body(payload)
        url = self._build_url(path, query)
        headers = self.sign(method, url, body=body)
        logger.info("upstream %s %s", method.upper(), path)
        response = await self._http.request(
            method.upper(),
            url,
            headers=headers,
            content=body if body is not None else None,
        )
        parsed = _parse_body(response)
        if response.status_code not in expected:
            message = _error_message(parsed, response.status_code)
            raise UpstreamError(message, response.status_code, parsed)
        return parsed, response.status_code

    async def get_plugin_statistics(self) -> Any:
        payload, _status = await self.request_json("GET", PLUGIN_STATISTICS_PATH)
        return payload

    async def list_builtin_models(self) -> Any:
        payload, _status = await self.request_json("GET", MODELS_PATH)
        return payload

    async def list_activities(self, channel: str = "IDE") -> Any:
        payload, _status = await self.request_json(
            "GET",
            DELIVERY_PATH,
            query={"channel": channel},
        )
        return payload

    async def claim_campaign(self, campaign_id: Any, channel: str = "IDE") -> tuple[Any, int]:
        return await self.request_json(
            "POST",
            CLAIM_PATH,
            payload={"campaignId": campaign_id, "channel": channel},
            expected=tuple(range(200, 500)),
        )

    async def confirm_campaign(self, campaign_id: Any) -> tuple[Any, int]:
        return await self.request_json(
            "POST",
            CONFIRM_PATH,
            payload={"campaignId": campaign_id},
            expected=tuple(range(200, 500)),
        )

    async def post_chat(self, payload: dict[str, Any]) -> Any:
        body, _status = await self.request_json("POST", CHAT_PATH, payload=payload)
        return body

    def stream_chat(self, payload: dict[str, Any]) -> httpx.Response:
        """Return an async context manager for a signed streaming POST."""
        body = json_body(payload)
        url = self._build_url(CHAT_PATH)
        headers = self.sign("POST", url, body=body)
        headers.setdefault("Accept", "text/event-stream")
        timeout = httpx.Timeout(
            connect=self._settings.http_timeout,
            read=self._settings.http_stream_timeout,
            write=self._settings.http_timeout,
            pool=self._settings.http_timeout,
        )
        logger.info("upstream POST %s (stream)", CHAT_PATH)
        return self._http.stream("POST", url, headers=headers, content=body, timeout=timeout)

    async def claim_opengw_benefit(self) -> dict[str, Any]:
        """Limited-time model benefit claim, as configured in the official CLI.

        Official CLI 26.11.11 (China production) uses
        POST https://opengw.developer.huaweicloud.com/api/v1/benefit/claim
        with an empty JSON object, SDK-HMAC-SHA256, optional X-Security-Token,
        and treats error_code == "0000" as success.
        """
        import uuid

        base = self._settings.opengw_base_url
        if not base:
            return {
                "status": "skipped",
                "reason": "OPENGW_BASE_URL is empty.",
            }
        url = base.rstrip("/") + BENEFIT_CLAIM_PATH
        body = json_body({})
        extra = {"x-snap-traceid": uuid.uuid4().hex}
        headers = self.sign("POST", url, body=body, headers=extra)
        response = await self._http.post(url, headers=headers, content=body)
        parsed = _parse_body(response)
        code = None
        if isinstance(parsed, dict):
            code = parsed.get("error_code") or parsed.get("errorCode") or parsed.get("code")
        success = str(code) == "0000"
        return {
            "status": "claimed" if success else "error",
            "http_status": response.status_code,
            "error_code": code,
            "payload": parsed if isinstance(parsed, (dict, list)) else {"raw": str(parsed)},
        }



def _parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type or response.content[:1] in (b"{", b"["):
        try:
            return response.json()
        except ValueError:
            return response.text
    return response.text


def _error_message(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error_msg", "errorMsg", "msg", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"]
    return f"Upstream request failed with HTTP {status_code}"
