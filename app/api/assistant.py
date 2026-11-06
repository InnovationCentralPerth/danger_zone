"""REST API for the natural-language assistant ("Ask Safety Sentry")."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import assistant

router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=assistant.MAX_QUESTION_CHARS)
    feed_cleared_after_id: int | None = Field(
        default=None,
        description="Newest incident id hidden by Clear feed, so 'since' windows respect it.",
    )


@router.post("/ask", summary="Ask a plain-English question about what has been seen")
async def ask(body: AskRequest) -> dict:
    try:
        return await assistant.ask(body.question, body.feed_cleared_after_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
