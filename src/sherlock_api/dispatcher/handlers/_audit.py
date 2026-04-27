from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sherlock_api.db.models import TgInteraction


@dataclass
class AuditEvent:
    kind: str
    direction: str
    text: str | None = None
    tg_msg_id: int | None = None
    payload: dict[str, Any] | None = None
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BufferingAuditSink:
    cap: int = 1000
    events: list[AuditEvent] = field(default_factory=list)

    def __call__(self, event: dict[str, Any]) -> None:
        direction = str(event.get("direction") or "meta")
        kind = event.get("kind")
        if not kind:
            kind = f"{direction}.event"
        text = event.get("text") or event.get("text_prefix")
        tg_msg_id = event.get("msg_id")

        payload = {
            k: v
            for k, v in event.items()
            if k not in {"direction", "kind", "text", "text_prefix", "msg_id"} and v is not None
        }

        ev = AuditEvent(
            kind=str(kind)[:32],
            direction=direction[:8],
            text=text,
            tg_msg_id=int(tg_msg_id) if tg_msg_id is not None else None,
            payload=payload or None,
        )
        if len(self.events) >= self.cap:
            self.events.pop(1 if len(self.events) > 1 else 0)
        self.events.append(ev)


async def flush_audit(
    session: AsyncSession,
    sink: BufferingAuditSink,
    *,
    task_id: uuid.UUID | None,
    account_id: int | None,
) -> int:
    if not sink.events:
        return 0
    rows = [
        TgInteraction(
            task_id=task_id,
            account_id=account_id,
            kind=e.kind,
            direction=e.direction,
            tg_msg_id=e.tg_msg_id,
            text=e.text,
            payload=e.payload,
            at=e.at,
        )
        for e in sink.events
    ]
    session.add_all(rows)
    await session.flush()
    written = len(rows)
    sink.events.clear()
    return written


__all__ = ["AuditEvent", "BufferingAuditSink", "flush_audit"]
