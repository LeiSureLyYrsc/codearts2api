"""Map upstream chat payloads to OpenAI-compatible responses.

The official public API catalog does not document the snap-access chat
schema. This mapper keeps upstream fields when they already look like
OpenAI Chat Completions, fills only the standard envelope fields when
they are missing, and unwraps a single obvious wrapper (data/result).
It does not invent tool-call or finish-reason behavior.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

OPENAI_COMPLETION_OBJECT = "chat.completion"
OPENAI_CHUNK_OBJECT = "chat.completion.chunk"


def unwrap_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if "choices" in payload:
        return payload
    for key in ("data", "result", "response"):
        inner = payload.get(key)
        if isinstance(inner, dict) and ("choices" in inner or "message" in inner):
            return inner
    return payload


def _ensure_id(payload: dict[str, Any], prefix: str) -> str:
    existing = payload.get("id")
    if isinstance(existing, str) and existing:
        return existing
    return f"{prefix}-{int(time.time())}"


def normalize_completion(payload: Any, model: str) -> dict[str, Any]:
    """Normalize a non-stream chat response to an OpenAI-like object."""
    body = unwrap_payload(payload)
    if not isinstance(body, dict):
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": OPENAI_COMPLETION_OBJECT,
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": str(body)},
                    "finish_reason": "stop",
                }
            ],
        }

    choices = body.get("choices")
    if not isinstance(choices, list):
        # A single assistant message at the top level.
        if isinstance(body.get("message"), dict):
            choices = [{"index": 0, "message": body["message"], "finish_reason": body.get("finish_reason")}]
        else:
            choices = list(choices) if choices else []

    normalized_choices: list[dict[str, Any]] = []
    for index, raw in enumerate(choices):
        if not isinstance(raw, dict):
            continue
        choice = dict(raw)
        choice.setdefault("index", index)
        if "message" not in choice and isinstance(choice.get("delta"), dict):
            # Non-stream payload accidentally used delta; promote to message.
            delta = choice["delta"]
            message = {"role": delta.get("role") or "assistant"}
            if "content" in delta:
                message["content"] = delta.get("content")
            if "tool_calls" in delta:
                message["tool_calls"] = delta.get("tool_calls")
            choice["message"] = message
        if "message" in choice and isinstance(choice["message"], dict):
            choice["message"].setdefault("role", "assistant")
        choice.setdefault("finish_reason", choice.get("finish_reason"))
        normalized_choices.append(choice)

    result = dict(body)
    result["id"] = _ensure_id(body, "chatcmpl")
    result["object"] = OPENAI_COMPLETION_OBJECT
    result.setdefault("created", int(time.time()))
    result["model"] = body.get("model") or model
    result["choices"] = normalized_choices
    if "usage" in body:
        result["usage"] = body["usage"]
    return result


def normalize_chunk(payload: Any, model: str) -> dict[str, Any]:
    """Normalize one SSE JSON payload to an OpenAI chat.completion.chunk."""
    body = unwrap_payload(payload)
    if not isinstance(body, dict):
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": OPENAI_CHUNK_OBJECT,
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": str(body)},
                    "finish_reason": None,
                }
            ],
        }

    choices = body.get("choices")
    if not isinstance(choices, list):
        if isinstance(body.get("delta"), dict):
            choices = [{"index": 0, "delta": body["delta"], "finish_reason": body.get("finish_reason")}]
        elif "content" in body and "choices" not in body:
            choices = [
                {
                    "index": 0,
                    "delta": {"content": body.get("content")},
                    "finish_reason": body.get("finish_reason"),
                }
            ]
        else:
            choices = []

    normalized_choices: list[dict[str, Any]] = []
    for index, raw in enumerate(choices):
        if not isinstance(raw, dict):
            continue
        choice = dict(raw)
        choice.setdefault("index", index)
        if "delta" not in choice and isinstance(choice.get("message"), dict):
            message = choice["message"]
            delta = {}
            if "role" in message:
                delta["role"] = message.get("role")
            if "content" in message:
                delta["content"] = message.get("content")
            if "tool_calls" in message:
                delta["tool_calls"] = message.get("tool_calls")
            choice["delta"] = delta
        choice.setdefault("finish_reason", choice.get("finish_reason"))
        normalized_choices.append(choice)

    result = dict(body)
    result["id"] = _ensure_id(body, "chatcmpl")
    result["object"] = OPENAI_CHUNK_OBJECT
    result.setdefault("created", int(time.time()))
    result["model"] = body.get("model") or model
    result["choices"] = normalized_choices
    return result


def iter_sse_data_lines(text_chunk: str) -> Iterator[str]:
    """Yield individual SSE lines (without the trailing newline)."""
    for raw in text_chunk.splitlines():
        yield raw


def format_sse_json(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_sse_done() -> str:
    return "data: [DONE]\n\n"


def openai_error(message: str, status_code: int = 500, code: str | None = None) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": "api_error" if status_code >= 500 else "invalid_request_error",
            "code": code or str(status_code),
        }
    }
