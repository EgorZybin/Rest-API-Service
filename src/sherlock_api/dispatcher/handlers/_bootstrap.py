from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.errors import ChatWriteForbiddenError, FloodWaitError
from telethon.tl.custom import Message

from sherlock_api.db.models import Account
from sherlock_api.dispatcher.handlers.base import (
    HandlerRateLimitError,
    HandlerSubscriptionError,
)
from sherlock_api.logging import get_logger

log = get_logger(__name__)

BOOTSTRAP_REPLY_TIMEOUT = 15.0


async def ensure_bootstrapped(
    *,
    client: TelegramClient,
    bot_id: int,
    account: Account,
    force: bool = False,
) -> bool:
    if account.bootstrapped_at is not None and not force:
        return False

    reply_event = asyncio.Event()
    first_reply: list[Message] = []

    @events.register(events.NewMessage(from_users=bot_id))
    async def _on_reply(event: events.NewMessage.Event) -> None:
        if not first_reply:
            first_reply.append(event.message)
        reply_event.set()

    client.add_event_handler(_on_reply)
    try:
        try:
            await client.send_message(bot_id, "/start")
        except ChatWriteForbiddenError as e:
            raise HandlerSubscriptionError(f"cannot write /start to bot: {type(e).__name__}") from e
        except FloodWaitError as e:
            raise HandlerRateLimitError(f"FloodWait {e.seconds}s while bootstrapping") from e

        try:
            await asyncio.wait_for(reply_event.wait(), timeout=BOOTSTRAP_REPLY_TIMEOUT)
        except asyncio.TimeoutError as e:
            raise HandlerSubscriptionError(
                f"bot silent for {BOOTSTRAP_REPLY_TIMEOUT:.0f}s after /start — "
                "account likely not whitelisted by the bot operator"
            ) from e

        now = datetime.now(timezone.utc)
        account.bootstrapped_at = now
        account.last_bot_ack_at = now
        log.info(
            "flow.bootstrap.ok",
            account_id=account.id,
            bot_id=bot_id,
            reply_msg_id=first_reply[0].id if first_reply else None,
        )
        return True
    finally:
        try:
            client.remove_event_handler(_on_reply)
        except Exception:
            pass


__all__ = ["ensure_bootstrapped", "BOOTSTRAP_REPLY_TIMEOUT"]
