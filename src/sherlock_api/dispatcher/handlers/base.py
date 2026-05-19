from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from sherlock_api.db.models import Account, Task

if TYPE_CHECKING:
    from telethon import TelegramClient

    from sherlock_api.dispatcher.handlers._audit import BufferingAuditSink


@dataclass(slots=True)
class HandlerContext:
    client: "TelegramClient"
    account: Account
    task: Task
    bot_username: str
    audit: "BufferingAuditSink | None" = None
    session: AsyncSession | None = None


@dataclass(slots=True)
class HandlerOutcome:
    data: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)


class HandlerError(RuntimeError):
    code: str = "handler_error"


class HandlerPermanentError(HandlerError):
    code = "handler_permanent"


class HandlerRateLimitError(HandlerError):
    code = "handler_rate_limit"


class HandlerSubscriptionError(HandlerPermanentError):
    code = "subscription_expired"


class HandlerResolveAccountQuotaError(HandlerError):
    code = "resolve_account_quota"


HandlerFn = Callable[[HandlerContext], Awaitable[HandlerOutcome]]

HANDLERS: dict[str, HandlerFn] = {}


def register_handler(scenario: str) -> Callable[[HandlerFn], HandlerFn]:

    def _decorator(fn: HandlerFn) -> HandlerFn:
        if scenario in HANDLERS:
            raise RuntimeError(f"handler for scenario {scenario!r} already registered")
        HANDLERS[scenario] = fn
        return fn

    return _decorator
