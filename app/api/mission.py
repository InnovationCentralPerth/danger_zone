"""REST API for the global mission context.

Lets a user (or an external agent) set the PPE requirements that the
``ppe_compliance`` agent will check at run time, in plain English.

Mounted under ``/agents`` so the demo URL surface stays cohesive:

    GET    /agents/context   -> current AgentContext or null
    POST   /agents/context   -> {"mission": "..."} -> parse + persist
    DELETE /agents/context   -> revert to YAML default

The POST handler triggers exactly one cheap LLM call to extract structured
items from the natural-language input. Subsequent snapshot evaluations reuse
the persisted context with no additional parsing cost.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.integrations.vlm import VLMError
from app.services import mission_context

router = APIRouter()
logger = logging.getLogger(__name__)


class AgentContextDTO(BaseModel):
    raw_input: str
    required_items: list[str]
    context_note: str
    severity_hint: str
    parsed_at: str
    model_id: str


class ContextStatusDTO(BaseModel):
    active: bool
    context: AgentContextDTO | None = None


class SetMissionRequest(BaseModel):
    mission: str = Field(
        ...,
        min_length=1,
        max_length=mission_context.MAX_INPUT_CHARS,
        description=(
            "Natural-language description of the PPE workers must wear. "
            f"Up to {mission_context.MAX_INPUT_CHARS} characters; the LLM "
            f"extracts up to {mission_context.MAX_ITEMS} items."
        ),
    )


@router.get(
    "/context",
    summary="Get the active mission context",
    response_model=ContextStatusDTO,
)
async def get_context() -> ContextStatusDTO:
    ctx = mission_context.get_context()
    if ctx is None:
        return ContextStatusDTO(active=False)
    return ContextStatusDTO(active=True, context=AgentContextDTO(**ctx.as_dict()))


@router.post(
    "/context",
    summary="Set the mission context from a natural-language instruction",
    response_model=ContextStatusDTO,
)
async def set_context(body: SetMissionRequest) -> ContextStatusDTO:
    try:
        ctx = await mission_context.set_context(body.mission)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except VLMError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e
    return ContextStatusDTO(active=True, context=AgentContextDTO(**ctx.as_dict()))


@router.delete(
    "/context",
    summary="Clear the mission context (PPE agent reverts to YAML default)",
    response_model=ContextStatusDTO,
)
async def clear_context() -> ContextStatusDTO:
    await mission_context.clear_context()
    return ContextStatusDTO(active=False)
