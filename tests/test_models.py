"""Model list filtering: enable / display_enabled and prefix stripping."""

from __future__ import annotations

from codearts2api.models_catalog import (
    filter_and_map_models,
    is_model_exposed,
    strip_codearts_prefix,
)


def test_strip_codearts_prefix() -> None:
    assert strip_codearts_prefix("codearts/GLM-5.2") == "GLM-5.2"
    assert strip_codearts_prefix("GLM-5.2") == "GLM-5.2"
    assert strip_codearts_prefix("codearts/") == ""


def test_enable_must_be_true() -> None:
    assert is_model_exposed({"id": "A", "enable": True}) is True
    assert is_model_exposed({"id": "A", "enable": False}) is False
    assert is_model_exposed({"id": "A"}) is False
    assert is_model_exposed({"id": "A", "enabled": True}) is True
    assert is_model_exposed({"id": "A", "enabled": False}) is False


def test_display_enabled_honored_when_present() -> None:
    assert is_model_exposed({"id": "A", "enable": True, "display_enabled": True}) is True
    assert is_model_exposed({"id": "A", "enable": True, "display_enabled": False}) is False
    # Field absent: enable=true is enough.
    assert is_model_exposed({"id": "A", "enable": True}) is True


def test_filter_and_map_unwraps_and_deduplicates() -> None:
    payload = {
        "data": {
            "models": [
                {"id": "codearts/GLM-5.2", "enable": True, "display_enabled": True},
                {"id": "Hidden", "enable": False},
                {"id": "NoDisplay", "enable": True, "display_enabled": False},
                {"model_id": "Visible-2", "enable": True},
                {"id": "GLM-5.2", "enable": True},  # duplicate after prefix strip
            ]
        }
    }
    models = filter_and_map_models(payload, created=1)
    ids = [item["id"] for item in models]
    assert ids == ["GLM-5.2", "Visible-2"]
    assert models[0]["object"] == "model"
    assert models[0]["owned_by"] == "codearts"
