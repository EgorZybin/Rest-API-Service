from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sherlock_api.dispatcher.handlers.base import HandlerContext, HandlerOutcome, register_handler


@register_handler("noop")
async def noop_handler(ctx: HandlerContext) -> HandlerOutcome:
    delay_ms = int(ctx.task.input.get("delay_ms", 0) or 0)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)
    return HandlerOutcome(
        data={
            "echo": ctx.task.input,
            "account_id": ctx.account.id,
            "account_phone": ctx.account.phone,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        },
        meta={"handler": "noop"},
    )
