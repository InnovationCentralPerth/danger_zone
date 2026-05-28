"""VLM provider abstraction.

Default implementation talks to GitHub Models via its OpenAI-compatible API.
A small registry is exposed so the dashboard can switch models at runtime.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings
from app.services.config_loader import load_models

logger = logging.getLogger(__name__)

# GitHub Models endpoint (OpenAI-compatible). Auth via GITHUB_TOKEN.
GITHUB_MODELS_BASE_URL = "https://models.github.ai/inference"


class VLMError(RuntimeError):
    pass


@dataclass
class VLMResult:
    """A parsed VLM response. ``raw`` keeps the model's full text for traces."""

    raw: str
    parsed: dict[str, Any]
    model_id: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class GitHubModelsProvider:
    """OpenAI-compatible client pointed at GitHub Models."""

    def __init__(self, token: str, default_model: str) -> None:
        if not token:
            raise VLMError("GITHUB_TOKEN is empty — set it in .env")
        self._client = AsyncOpenAI(api_key=token, base_url=GITHUB_MODELS_BASE_URL)
        self._default_model = default_model

    async def analyse_image(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        model_id: str | None = None,
        max_tokens: int = 1024,
        image_mime: str = "image/jpeg",
    ) -> VLMResult:
        """Send an image + prompt; expect JSON back. Raises VLMError on failure."""
        import time

        model = model_id or self._default_model
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_mime};base64,{b64}"

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a security analyst reviewing CCTV snapshots. "
                    "Always respond with a single valid JSON object — no prose, "
                    "no markdown code fences."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]

        start = time.monotonic()
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception as e:  # noqa: BLE001
            raise VLMError(f"VLM call failed ({model}): {e}") from e

        elapsed_ms = int((time.monotonic() - start) * 1000)
        text = resp.choices[0].message.content or "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Some models still wrap JSON in ```json fences despite response_format.
            cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
            try:
                parsed = json.loads(cleaned.strip())
            except json.JSONDecodeError as e:
                raise VLMError(f"VLM returned non-JSON: {text[:200]!r}") from e

        usage = resp.usage
        return VLMResult(
            raw=text,
            parsed=parsed,
            model_id=model,
            latency_ms=elapsed_ms,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )


# ---- model registry ---------------------------------------------------------


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    vision: bool
    max_tokens: int


def list_models() -> list[ModelInfo]:
    """Return all configured models from config/models.yaml."""
    cfg = load_models()
    return [
        ModelInfo(
            id=m["id"],
            label=m["label"],
            vision=bool(m.get("vision", False)),
            max_tokens=int(m.get("max_tokens", 1024)),
        )
        for m in cfg.get("models", [])
    ]


def list_vision_models() -> list[ModelInfo]:
    return [m for m in list_models() if m.vision]


def default_model_id() -> str:
    """Resolve the active model id.

    Precedence: MODEL_ID env > config/models.yaml `default` > first vision model.
    """
    env_id = get_settings().model_id
    if env_id:
        return env_id
    cfg = load_models()
    default = cfg.get("default")
    if default:
        return str(default)
    vision = list_vision_models()
    if vision:
        return vision[0].id
    raise VLMError("No models configured in config/models.yaml")


# ---- singleton -------------------------------------------------------------

_provider: GitHubModelsProvider | None = None
_active_model_id: str | None = None


def get_provider() -> GitHubModelsProvider:
    global _provider
    if _provider is None:
        settings = get_settings()
        _provider = GitHubModelsProvider(
            token=settings.github_token,
            default_model=default_model_id(),
        )
    return _provider


def set_active_model(model_id: str) -> None:
    """Set the model used for subsequent VLM calls (runtime override)."""
    global _active_model_id
    valid_ids = {m.id for m in list_models()}
    if model_id not in valid_ids:
        raise VLMError(f"Unknown model: {model_id}. Add it to config/models.yaml")
    _active_model_id = model_id
    logger.info("Active VLM model set to %s", model_id)


def get_active_model() -> str:
    return _active_model_id or default_model_id()


def reset_provider() -> None:
    """Test hook."""
    global _provider, _active_model_id
    _provider = None
    _active_model_id = None


# ---- helpers ---------------------------------------------------------------


def load_image(path: str | Path) -> bytes:
    return Path(path).read_bytes()
