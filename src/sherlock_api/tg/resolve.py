from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import User

from sherlock_api.config import Settings, get_settings
from sherlock_api.db.enums import AccountStatus
from sherlock_api.db.models import Account
from sherlock_api.dispatcher.handlers.base import (
    HandlerPermanentError,
    HandlerRateLimitError,
    HandlerResolveAccountQuotaError,
)
from sherlock_api.logging import get_logger
from sherlock_api.parsers import SherlockResult

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

_TME_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/(?P<slug>[A-Za-z0-9_]{5,32})/?$",
    re.IGNORECASE,
)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

_POOL_STATUSES = {
    AccountStatus.idle,
    AccountStatus.busy,
    AccountStatus.paused,
    AccountStatus.limited,
    AccountStatus.subscription_expired,
}


@dataclass(slots=True)
class ResolvedTelegramUser:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_bot: bool
    is_premium: bool
    via_username_resolve: bool

    def to_result_dict(self) -> dict[str, Any]:
        title = f"💬 ID: {self.user_id}"
        raw_fields: dict[str, Any] = {"tg_user_id": str(self.user_id)}
        if self.username:
            raw_fields["username"] = f"@{self.username}"
        if self.first_name:
            raw_fields["first_name"] = self.first_name
        if self.last_name:
            raw_fields["last_name"] = self.last_name
        if self.is_premium:
            raw_fields["is_premium"] = True

        profile_url = f"https://t.me/{self.username}" if self.username else None
        result = SherlockResult(
            title=title,
            username=self.username,
            first_name=self.first_name,
            last_name=self.last_name,
            profile_url=profile_url,
            raw_fields=raw_fields,
        )
        return result.to_dict()


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_telegram_nick(nick: str) -> tuple[str | None, int | None]:
    """Return (username, user_id) — exactly one may be set."""
    raw = (nick or "").strip()
    if not raw:
        raise HandlerPermanentError("telegram nick is empty")

    if raw.startswith("@"):
        raw = raw[1:].strip()

    m = _TME_RE.match(raw)
    if m:
        return m.group("slug"), None

    if raw.isdigit():
        uid = int(raw)
        if uid <= 0:
            raise HandlerPermanentError(f"invalid telegram user id: {nick!r}")
        return None, uid

    if _USERNAME_RE.fullmatch(raw):
        return raw, None

    raise HandlerPermanentError(f"cannot parse telegram nick/username: {nick!r}")


def resolve_day_cap_reached(account: Account, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    limit = settings.account_resolve_username_per_day
    if limit <= 0:
        return True

    start = account.resolve_window_started_at
    if start is None:
        return False

    now = datetime.now(timezone.utc)
    if now - _aware(start) >= timedelta(hours=24):
        return False

    return (account.resolve_requests_today or 0) >= limit


def note_resolve_username_request(account: Account) -> None:
    now = datetime.now(timezone.utc)
    start = account.resolve_window_started_at
    if start is None or now - _aware(start) >= timedelta(hours=24):
        account.resolve_window_started_at = now
        account.resolve_requests_today = 1
    else:
        account.resolve_requests_today = (account.resolve_requests_today or 0) + 1


async def any_account_has_resolve_quota(
    session: AsyncSession,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    if settings.account_resolve_username_per_day <= 0:
        return False

    rows = (
        await session.execute(select(Account).where(Account.status.in_(_POOL_STATUSES)))
    ).scalars().all()
    return any(not resolve_day_cap_reached(acc, settings) for acc in rows)


def _entity_to_resolved(entity: User, *, via_username_resolve: bool) -> ResolvedTelegramUser:
    if not isinstance(entity, User):
        raise HandlerPermanentError(
            f"resolved entity is not a user: {type(entity).__name__}"
        )
    if entity.deleted:
        raise HandlerPermanentError("telegram user is deleted")

    return ResolvedTelegramUser(
        user_id=int(entity.id),
        username=entity.username,
        first_name=entity.first_name,
        last_name=entity.last_name,
        is_bot=bool(entity.bot),
        is_premium=bool(getattr(entity, "premium", False)),
        via_username_resolve=via_username_resolve,
    )


async def resolve_telegram_nick(
    client: TelegramClient,
    nick: str,
) -> ResolvedTelegramUser:
    username, user_id = normalize_telegram_nick(nick)
    target: str | int = username if username is not None else user_id  # type: ignore[assignment]

    try:
        entity = await client.get_entity(target)
    except FloodWaitError as e:
        raise HandlerRateLimitError(f"FloodWait {e.seconds}s on resolve username") from e
    except UsernameNotOccupiedError as e:
        raise HandlerPermanentError(f"telegram username not occupied: {nick!r}") from e
    except UsernameInvalidError as e:
        raise HandlerPermanentError(f"telegram username invalid: {nick!r}") from e
    except ValueError as e:
        raise HandlerPermanentError(f"telegram nick not found: {nick!r}") from e
    except HandlerPermanentError:
        raise
    except Exception as e:
        raise HandlerPermanentError(
            f"telegram resolve failed: {type(e).__name__}: {e}"
        ) from e

    return _entity_to_resolved(entity, via_username_resolve=username is not None)


async def try_resolve_for_nick_search(
    *,
    client: TelegramClient,
    account: Account,
    session: AsyncSession | None,
    nick: str,
) -> ResolvedTelegramUser | None:
    """Resolve via MTProto. Returns None when the whole pool is out of username-resolve quota."""
    settings = get_settings()
    username, user_id = normalize_telegram_nick(nick)

    if user_id is not None:
        log.debug(
            "tg.resolve.by_id",
            account_id=account.id,
            user_id=user_id,
        )
        return await resolve_telegram_nick(client, nick)

    if resolve_day_cap_reached(account, settings):
        if session is not None and await any_account_has_resolve_quota(session, settings):
            raise HandlerResolveAccountQuotaError(
                f"account {account.id} exhausted daily username resolve quota "
                f"({settings.account_resolve_username_per_day}/24h)"
            )
        log.info(
            "tg.resolve.pool_exhausted",
            account_id=account.id,
            limit=settings.account_resolve_username_per_day,
        )
        return None

    resolved = await resolve_telegram_nick(client, nick)
    if resolved.via_username_resolve:
        note_resolve_username_request(account)
    return resolved
