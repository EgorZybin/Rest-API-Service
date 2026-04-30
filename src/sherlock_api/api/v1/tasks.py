from __future__ import annotations

import asyncio
import json
import uuid
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sherlock_api.api.v1.schemas.tasks import TaskOut, TgInteractionOut
from sherlock_api.auth import require_api_key
from sherlock_api.db.enums import TaskStatus
from sherlock_api.db.models import Task, TgInteraction
from sherlock_api.db.notify import get_notify_hub
from sherlock_api.db.session import async_session_factory, get_session
from sherlock_api.logging import get_logger

router = APIRouter(prefix="/tasks", tags=["tasks"])
log = get_logger(__name__)

_TERMINAL_STATUSES = {
    TaskStatus.completed,
    TaskStatus.failed,
    TaskStatus.cancelled,
    TaskStatus.timeout,
}

_SSE_HEARTBEAT = b": ping\n\n"
_SSE_HEARTBEAT_INTERVAL = 15.0

_SSE_MAX_LIFETIME = 600.0


async def _get_task_scoped(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get(
    "/{task_id}",
    response_model=TaskOut,
    summary="Получить текущее состояние одной задачи",
)
async def get_task(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> Task:
    return await _get_task_scoped(session, task_id)


@router.get(
    "",
    response_model=list[TaskOut],
    summary="Список задач, доступных этому API-ключу",
)
async def list_tasks(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    scenario: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Task]:
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
    if status_filter is not None:
        stmt = stmt.where(Task.status == status_filter)
    if scenario is not None:
        stmt = stmt.where(Task.scenario == scenario)

    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


def _sse_event(event: str, data: dict) -> bytes:
    payload = json.dumps(data, default=str, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


async def _task_event_stream(
    request: Request,
    task_id: uuid.UUID,
) -> AsyncIterator[bytes]:
    factory: async_sessionmaker[AsyncSession] = async_session_factory()
    hub = get_notify_hub()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SSE_MAX_LIFETIME

    async with factory() as s:
        try:
            task = await _get_task_scoped(s, task_id)
        except HTTPException as exc:
            yield _sse_event("error", {"detail": exc.detail, "status": exc.status_code})
            return

    async with hub.stream(task_id) as q:
        async with factory() as s:
            task = await s.get(Task, task_id)
            if task is None:
                yield _sse_event("error", {"detail": "task gone"})
                return
            yield _sse_event(
                "result" if task.status in _TERMINAL_STATUSES else "status",
                TaskOut.model_validate(task).model_dump(),
            )
            if task.status in _TERMINAL_STATUSES:
                return

        last_status = task.status
        while True:
            if await request.is_disconnected():
                log.debug("sse.client_disconnect", task_id=str(task_id))
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                async with factory() as s:
                    task = await s.get(Task, task_id)
                    if task is not None:
                        yield _sse_event(
                            "status",
                            TaskOut.model_validate(task).model_dump(),
                        )
                return
            try:
                await asyncio.wait_for(
                    q.get(),
                    timeout=min(remaining, _SSE_HEARTBEAT_INTERVAL),
                )
                notified = True
            except asyncio.TimeoutError:
                notified = False

            if not notified:
                yield _SSE_HEARTBEAT
                continue

            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

            async with factory() as s:
                task = await s.get(Task, task_id)
                if task is None:
                    yield _sse_event("error", {"detail": "task gone"})
                    return
                out = TaskOut.model_validate(task).model_dump()
                if task.status in _TERMINAL_STATUSES:
                    yield _sse_event("result", out)
                    return
                if task.status != last_status:
                    yield _sse_event("status", out)
                    last_status = task.status


@router.get(
    "/{task_id}/stream",
    summary="SSE-поток изменений состояния задачи (закрывается на финальном статусе)",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE stream. Events: status, result, error.",
        }
    },
)
async def stream_task(
    task_id: uuid.UUID,
    request: Request,
    _: Annotated[None, Depends(require_api_key)],
) -> StreamingResponse:
    return StreamingResponse(
        _task_event_stream(request, task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/{task_id}/interactions",
    response_model=list[TgInteractionOut],
    summary="Журнал всех TG-событий по задаче",
)
async def get_task_interactions(
    task_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[TgInteraction]:
    await _get_task_scoped(session, task_id)
    rows = (
        (
            await session.execute(
                select(TgInteraction)
                .where(TgInteraction.task_id == task_id)
                .order_by(TgInteraction.id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
