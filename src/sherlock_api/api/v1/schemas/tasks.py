from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sherlock_api.db.enums import TaskStatus


class TaskEnqueueCommon(BaseModel):
    priority: int = Field(
        default=0,
        ge=-100,
        le=100,
        description=(
            "Чем выше, тем раньше задача будет взята из общей очереди. "
            "Отрицательные значения понижают приоритет."
        ),
    )
    max_attempts: int = Field(default=3, ge=1, le=10)
    webhook_url: str | None = Field(
        default=None,
        description=(
            "Если задано, диспетчер отправит POST с финальным состоянием "
            "задачи на этот URL после завершения или финальной ошибки."
        ),
        max_length=512,
    )


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scenario: str
    status: TaskStatus
    priority: int
    attempts: int
    max_attempts: int
    account_id: int | None
    input: dict[str, Any]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    webhook_url: str | None = None
    webhook_delivered_at: datetime | None = None
    webhook_attempts: int = 0
    webhook_next_attempt_at: datetime | None = None
    webhook_error: str | None = None


class TaskEnqueueResponse(BaseModel):
    id: uuid.UUID
    scenario: str
    status: TaskStatus
    priority: int
    created_at: datetime
    status_url: str = Field(description="Путь (без хоста) для опроса финального статуса задачи.")


class TgInteractionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    direction: str
    tg_msg_id: int | None
    text: str | None
    payload: dict[str, Any] | None
    at: datetime
