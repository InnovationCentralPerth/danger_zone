"""Tests for the VLM provider abstraction and model registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations import vlm


@pytest.fixture(autouse=True)
def reset_vlm():
    vlm.reset_provider()
    yield
    vlm.reset_provider()


def test_list_models_from_config():
    models = vlm.list_models()
    ids = {m.id for m in models}
    # The raw catalogue keeps cloud entries for dev, but marks them non-local.
    assert "openai/gpt-4o-mini" in ids
    assert all(isinstance(m.vision, bool) for m in models)
    assert next(m for m in models if m.id == "qwen3-vl-235b-awq").local is True
    assert next(m for m in models if m.id == "openai/gpt-4o-mini").local is False


def test_available_models_exclude_cloud_by_default():
    ids = {m.id for m in vlm.list_available_models()}
    assert "qwen3-vl-235b-awq" in ids
    assert "openai/gpt-4o-mini" not in ids


def test_list_vision_models_filters_text_only():
    vision = vlm.list_vision_models()
    assert all(m.vision for m in vision)
    assert "meta/llama-3.3-70b-instruct" not in {m.id for m in vision}


def test_default_model_id_uses_config_default(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "")
    from app import config

    config.get_settings.cache_clear()
    assert vlm.default_model_id() == "qwen3-vl-235b-awq"


def test_set_active_model_rejects_unknown():
    with pytest.raises(vlm.VLMError):
        vlm.set_active_model("nope/never")


def test_set_active_model_rejects_cloud_by_default():
    with pytest.raises(vlm.VLMError):
        vlm.set_active_model("openai/gpt-4o")


def test_set_active_model_round_trip():
    vlm.set_active_model("qwen3-vl-235b-awq")
    assert vlm.get_active_model() == "qwen3-vl-235b-awq"


def test_cloud_models_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv("ALLOW_CLOUD_MODELS", "true")
    from app import config

    config.get_settings.cache_clear()
    try:
        ids = {m.id for m in vlm.list_available_models()}
        assert "openai/gpt-4o-mini" in ids
        vlm.set_active_model("openai/gpt-4o")
        assert vlm.get_active_model() == "openai/gpt-4o"
    finally:
        config.get_settings.cache_clear()
        vlm.reset_provider()


async def test_analyse_image_parses_json(monkeypatch):
    # Stub AsyncOpenAI so no network call is made.
    mock_resp = MagicMock()
    payload = '{"person_in_zone": true, "confidence": 0.91}'
    mock_resp.choices = [MagicMock(message=MagicMock(content=payload))]
    mock_resp.usage = MagicMock(prompt_tokens=42, completion_tokens=18)

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

    provider = vlm.GitHubModelsProvider(token="fake", default_model="openai/gpt-4o-mini")
    provider._client = mock_client  # type: ignore[assignment]

    result = await provider.analyse_image(b"\xff\xd8\xff", "is there a person?")
    assert result.parsed == {"person_in_zone": True, "confidence": 0.91}
    assert result.prompt_tokens == 42
    assert result.model_id == "openai/gpt-4o-mini"
    # Verify image was sent as data URL in the user message.
    call = mock_client.chat.completions.create.await_args
    content = call.kwargs["messages"][1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_analyse_image_strips_markdown_fences():
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content='```json\n{"ok": true}\n```'))]
    mock_resp.usage = None

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    provider = vlm.GitHubModelsProvider(token="fake", default_model="openai/gpt-4o-mini")
    provider._client = mock_client  # type: ignore[assignment]

    result = await provider.analyse_image(b"x", "prompt")
    assert result.parsed == {"ok": True}


async def test_analyse_image_raises_on_bad_json():
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="not json at all"))]
    mock_resp.usage = None
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    provider = vlm.GitHubModelsProvider(token="fake", default_model="openai/gpt-4o-mini")
    provider._client = mock_client  # type: ignore[assignment]

    with pytest.raises(vlm.VLMError):
        await provider.analyse_image(b"x", "prompt")


def test_provider_requires_token():
    with pytest.raises(vlm.VLMError):
        vlm.GitHubModelsProvider(token="", default_model="openai/gpt-4o-mini")
