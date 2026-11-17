"""AI Agent HTTP API — Phase 1."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import service as ai_service
from app.ai.budget import usage_summary
from app.auth import get_current_user
from app.db import get_db
from app.models import User

router = APIRouter(prefix="/api/ai", tags=["ai"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _require_admin(user: User) -> None:
    if user.role.lower() not in {"admin", "super_admin", "super admin", "management"}:
        raise HTTPException(status_code=403, detail="Admin or Management role required")


class CommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class SettingsPatch(BaseModel):
    enabled: bool | None = None
    monthly_budget_inr: float | None = Field(default=None, ge=0, le=1_000_000)
    daily_budget_inr: float | None = Field(default=None, ge=0, le=100_000)
    max_output_tokens: int | None = Field(default=None, ge=64, le=4096)
    model: str | None = Field(default=None, max_length=80)


class SessionPatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@router.get("/settings")
async def get_settings(db: Db, user: CurrentUser) -> dict[str, Any]:
    _ = user
    data = await ai_service.settings_public(db)
    await db.commit()
    return data


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, db: Db, user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    data = await ai_service.patch_settings(db, user, body.model_dump(exclude_unset=True))
    await db.commit()
    return data


@router.get("/usage")
async def get_usage(
    db: Db,
    user: CurrentUser,
    recent_limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _ = user
    return await usage_summary(db, recent_limit=recent_limit)


@router.post("/command")
async def post_command(body: CommandIn, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Accept immediately (status=running), finish in background so leaving /work does not abort."""
    try:
        result = await ai_service.accept_command(db, user, body.command, body.session_id)
        # accept_command already commits so pollers see the running run.
        if result.get("status") == "running":
            ai_service.schedule_complete_command(str(result["id"]))
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sessions")
async def get_sessions(
    db: Db,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Conversation list: one row per session (follow-ups update the same row)."""
    return await ai_service.list_sessions(db, user, limit=limit)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    row = await ai_service.get_session(db, user, session_id)
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


@router.patch("/sessions/{session_id}")
async def patch_session(session_id: str, body: SessionPatch, db: Db, user: CurrentUser) -> dict[str, Any]:
    """Rename conversation title shown on the /work session list."""
    try:
        row = await ai_service.patch_session_title(db, user, session_id, title=body.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.commit()
    return row


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, db: Db, user: CurrentUser) -> dict[str, str]:
    """Delete a conversation from the /work session list."""
    ok = await ai_service.delete_session(db, user, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.commit()
    return {"status": "deleted", "session_id": session_id}


@router.delete("/sessions/{session_id}/runs/{run_id}/messages/{side}")
async def delete_run_message(
    session_id: str,
    run_id: str,
    side: str,
    db: Db,
    user: CurrentUser,
) -> dict[str, Any]:
    """Soft-delete a You or assistant bubble in a thread turn (per-message ⋮ Delete)."""
    try:
        row = await ai_service.delete_run_message(
            db, user, session_public_id=session_id, run_public_id=run_id, side=side
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    await db.commit()
    return row


@router.get("/runs")
async def get_runs(
    db: Db,
    user: CurrentUser,
    limit: int = Query(default=30, ge=1, le=100),
) -> list[dict[str, Any]]:
    return await ai_service.list_runs(db, user, limit=limit)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: Db, user: CurrentUser) -> dict[str, Any]:
    row = await ai_service.get_run(db, user, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    return row
