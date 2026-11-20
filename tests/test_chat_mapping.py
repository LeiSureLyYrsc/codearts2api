"""OpenAI-compatible mapping for slightly different upstream field layouts."""

from __future__ import annotations

from codearts2api.chat import normalize_chunk, normalize_completion, unwrap_payload


def test_passthrough_openai_like_completion() -> None:
    raw = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 111,
        "model": "GLM-5.2",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    out = normalize_completion(raw, "GLM-5.2")
    assert out["id"] == "chatcmpl-1"
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "hello"
    assert out["usage"]["total_tokens"] == 3


def test_unwraps_data_wrapper() -> None:
    raw = {
        "data": {
            "id": "x",
            "choices": [{"message": {"content": "hi"}}],
        }
    }
    assert "choices" in unwrap_payload(raw)
    out = normalize_completion(raw, "M")
    assert out["model"] == "M"
    assert out["choices"][0]["message"]["content"] == "hi"
    assert out["choices"][0]["message"]["role"] == "assistant"
    assert out["object"] == "chat.completion"


def test_chunk_normalizes_message_to_delta() -> None:
    raw = {
        "id": "chatcmpl-2",
        "choices": [{"message": {"role": "assistant", "content": "he"}}],
    }
    out = normalize_chunk(raw, "GLM-5.2")
    assert out["object"] == "chat.completion.chunk"
    assert out["choices"][0]["delta"]["content"] == "he"
    assert out["model"] == "GLM-5.2"


def test_chunk_passthrough_delta() -> None:
    raw = {
        "choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": None}]
    }
    out = normalize_chunk(raw, "M")
    assert out["choices"][0]["delta"]["content"] == "llo"
    assert out["object"] == "chat.completion.chunk"
