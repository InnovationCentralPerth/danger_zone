"""Model registry API — list available VLM models and switch the active one."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.integrations.vlm import (
    VLMError,
    get_active_model,
    list_models,
    set_active_model,
)

router = APIRouter()


class ModelDTO(BaseModel):
    id: str
    label: str
    vision: bool
    max_tokens: int


class ActiveModelDTO(BaseModel):
    id: str


@router.get("/", summary="List configured VLM models")
async def get_models() -> list[ModelDTO]:
    return [
        ModelDTO(id=m.id, label=m.label, vision=m.vision, max_tokens=m.max_tokens)
        for m in list_models()
    ]


@router.get("/active", summary="Get the currently active VLM model id")
async def get_active() -> ActiveModelDTO:
    return ActiveModelDTO(id=get_active_model())


@router.post("/active", summary="Set the active VLM model id")
async def set_active(body: ActiveModelDTO) -> ActiveModelDTO:
    try:
        set_active_model(body.id)
    except VLMError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return ActiveModelDTO(id=get_active_model())
