from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sherlock_api.db.enums import AccountStatus, TaskStatus
from sherlock_api.db.models import Task
from sherlock_api.logging import get_logger

log = get_logger(__name__)


class TaskQueue:
    async def enqueue(
        self,
        session: AsyncSession,
        *,
        scenario: str,
        input_payload: dict[str, Any],
        priority: int = 0,
        max_attempts: int = 3,
        webhook_url: str | None = None,
        target_account_id: int | None = None,
    ) -> Task:
        task = Task(
            scenario=scenario,
            input=input_payload,
            priority=priority,
            max_attempts=max_attempts,
            webhook_url=webhook_url,
            status=TaskStatus.pending,
        )
        if target_account_id is not None:
            task.account_id = target_account_id
        session.add(task)
        await session.flush()
        return task

    async def pick_for_account(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        supported_scenarios: Iterable[str] | None = None,
        account_status: AccountStatus | None = None,
    ) -> Task | None:
        scenarios = list(supported_scenarios) if supported_scenarios else None

        stmt = (
            select(Task)
            .where(Task.status == TaskStatus.pending)
            .where((Task.account_id.is_(None)) | (Task.account_id == account_id))
            .order_by(Task.priority.desc(), Task.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if scenarios:
            stmt = stmt.where(Task.scenario.in_(scenarios))
        if account_status == AccountStatus.subscription_expired:
            stmt = stmt.where(Task.scenario == "nick_search")
            stmt = stmt.where(Task.input["search_in"].as_string() == "telegram")

        task = (await session.execute(stmt)).scalar_one_or_none()
        if task is None:
            return None

        now = datetime.now(timezone.utc)
        task.status = TaskStatus.running
        task.account_id = account_id
        task.attempts = (task.attempts or 0) + 1
        task.started_at = now
        await session.flush()
        return task

    async def complete(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        *,
        result: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc)
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status=TaskStatus.completed,
                result=result,
                error_code=None,
                error_message=None,
                finished_at=now,
            )
        )

    async def fail_final(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        *,
        error_code: str,
        error_message: str,
        status: TaskStatus = TaskStatus.failed,
    ) -> None:
        now = datetime.now(timezone.utc)
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status=status,
                error_code=error_code,
                error_message=error_message,
                finished_at=now,
            )
        )

    async def requeue(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        *,
        error_code: str,
        error_message: str,
        detach_account: bool = True,
    ) -> None:
        values: dict[str, Any] = {
            "status": TaskStatus.pending,
            "error_code": error_code,
            "error_message": error_message,
            "started_at": None,
        }
        if detach_account:
            values["account_id"] = None
        await session.execute(update(Task).where(Task.id == task_id).values(**values))

    async def reset_inflight(self, session: AsyncSession) -> int:
        now = datetime.now(timezone.utc)
        res = await session.execute(
            update(Task)
            .where(Task.status.in_([TaskStatus.running, TaskStatus.assigned]))
            .values(
                status=TaskStatus.pending,
                account_id=None,
                started_at=None,
                error_code="stale_inflight",
                error_message=f"reset on dispatcher startup at {now.isoformat()}",
            )
        )
        return res.rowcount or 0

    async def pending_count(self, session: AsyncSession) -> int:
        q = select(func.count(Task.id)).where(Task.status == TaskStatus.pending)
        return (await session.execute(q)).scalar_one()
