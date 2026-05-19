from __future__ import annotations

from pathlib import Path
from typing import Any

from sherlock_api.config import get_settings
from sherlock_api.db.enums import AccountStatus
from sherlock_api.db.models import Account
from sherlock_api.dispatcher.handlers._bootstrap import ensure_bootstrapped
from sherlock_api.dispatcher.handlers._flow import FlowResult, run_query_flow
from sherlock_api.dispatcher.handlers.base import (
    HandlerContext,
    HandlerError,
    HandlerOutcome,
    HandlerPermanentError,
    HandlerResolveAccountQuotaError,
    register_handler,
)
from sherlock_api.dispatcher.handlers.report_txt_fetch import merge_report_txt_if_present
from sherlock_api.dispatcher.handlers.schemas import (
    NickSearchInput,
    PhoneSearchInput,
    PhotoSearchInput,
    validate_input,
)
from sherlock_api.logging import get_logger
from sherlock_api.parsers import parse_simple_report
from sherlock_api.tg.resolve import (
    TelegramResolveNotFoundError,
    any_account_has_resolve_quota,
    try_resolve_for_nick_search,
)

log = get_logger(__name__)


def _normalize_vk_profile_url(raw: str) -> str:
    """Ссылка на профиль VK для отправки боту."""
    s = (raw or "").strip()
    if not s:
        raise HandlerPermanentError("vk profile input is empty")
    low = s.lower()
    if low.startswith("http://"):
        return "https://" + s[7:].lstrip("/")
    if low.startswith("https://"):
        return s
    if s.startswith("//"):
        return "https:" + s
    if "vk.com" in low or "vkontakte.ru" in low:
        return "https://" + s.lstrip("/")
    slug = s.lstrip("/").removeprefix("vk.com/").removeprefix("m.vk.com/")
    return f"https://vk.com/{slug.lstrip('/')}"


def _media_dir_for_task(task_id: Any) -> Path:
    settings = get_settings()
    root = Path(getattr(settings, "storage_dir", "storage")).resolve()
    return root / "tasks" / str(task_id)


async def _resolve_bot_id(ctx: HandlerContext) -> int:
    try:
        entity = await ctx.client.get_entity(ctx.bot_username)
    except Exception as e:
        raise HandlerPermanentError(
            f"could not resolve bot {ctx.bot_username!r}: {type(e).__name__}: {e}"
        ) from e
    bot_id = getattr(entity, "id", None)
    if bot_id is None:
        raise HandlerPermanentError(f"bot entity {ctx.bot_username!r} has no id")
    return int(bot_id)


async def _run_and_pack(
    *,
    ctx: HandlerContext,
    payload_text: str | None = None,
    payload_photo: str | Path | None = None,
    choice_button_text: str | None = None,
    max_pages: int,
    full_web_report: bool = False,
) -> HandlerOutcome:
    bot_id = await _resolve_bot_id(ctx)

    did_bootstrap = await ensure_bootstrapped(client=ctx.client, bot_id=bot_id, account=ctx.account)
    if did_bootstrap:
        log.info(
            "flow.bootstrap.first_time",
            account_id=ctx.account.id,
            bot_id=bot_id,
        )

    media_dir = _media_dir_for_task(ctx.task.id)
    flow: FlowResult = await run_query_flow(
        client=ctx.client,
        bot_id=bot_id,
        payload_text=payload_text,
        payload_photo=payload_photo,
        choice_button_text=choice_button_text,
        max_pages=max_pages,
        media_dir=media_dir,
        sink=ctx.audit,
    )

    if flow.last_bot_message_at is not None:
        ctx.account.last_bot_ack_at = flow.last_bot_message_at

    data = flow.to_merged()
    data["account_id"] = ctx.account.id
    data["bootstrap_performed"] = did_bootstrap

    meta: dict[str, Any] = {
        "pages": len(flow.pages),
        "has_media": any(p.media_path for p in flow.pages),
    }
    if full_web_report:
        report_url = reviews_url = None
        if flow.pages:
            sp = parse_simple_report(
                flow.pages[0].text or "",
                buttons=flow.pages[0].buttons,
            )
            report_url, reviews_url = sp.report_url, sp.reviews_url
        meta["has_report_url"] = report_url is not None
        if report_url:
            data["report_url"] = report_url
        if reviews_url:
            data["reviews_url"] = reviews_url
        await merge_report_txt_if_present(data)

    return HandlerOutcome(data=data, meta=meta)


@register_handler("phone_search")
async def phone_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: PhoneSearchInput = validate_input(
        "phone_search", ctx.task.input
    )
    return await _run_and_pack(
        ctx=ctx,
        payload_text=inp.phone,
        max_pages=inp.max_pages,
        full_web_report=True,
    )


def _can_use_sherlock_bot(account: Account) -> bool:
    return account.status != AccountStatus.subscription_expired


@register_handler("nick_search")
async def nick_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: NickSearchInput = validate_input(
        "nick_search", ctx.task.input
    )
    if inp.search_in == "telegram":
        try:
            resolved = await try_resolve_for_nick_search(
                client=ctx.client,
                account=ctx.account,
                session=ctx.session,
                nick=inp.nick,
            )
        except TelegramResolveNotFoundError as e:
            log.info(
                "nick_search.telegram.mtproto_not_found",
                account_id=ctx.account.id,
                reason=e.reason,
                nick=inp.nick,
            )
            out_data: dict[str, Any] = {
                "results": [],
                "resolve_method": "mtproto",
                "not_found": True,
                "not_found_reason": e.reason,
                "nick": inp.nick,
                "account_id": ctx.account.id,
            }
            if e.detail:
                out_data["not_found_detail"] = e.detail
            return HandlerOutcome(
                data=out_data,
                meta={
                    "resolve_method": "mtproto",
                    "not_found": True,
                    "not_found_reason": e.reason,
                    **({"not_found_detail": e.detail} if e.detail else {}),
                },
            )
        if resolved is not None:
            log.info(
                "nick_search.telegram.mtproto",
                account_id=ctx.account.id,
                user_id=resolved.user_id,
                username=resolved.username,
            )
            return HandlerOutcome(
                data={
                    "results": [resolved.to_result_dict()],
                    "resolve_method": "mtproto",
                    "account_id": ctx.account.id,
                },
                meta={"resolve_method": "mtproto", "via_username_resolve": resolved.via_username_resolve},
            )

        if not _can_use_sherlock_bot(ctx.account):
            if ctx.session is not None and await any_account_has_resolve_quota(ctx.session):
                raise HandlerResolveAccountQuotaError(
                    f"account {ctx.account.id} cannot use sherlock bot; "
                    "trying another account for mtproto resolve"
                )
            raise HandlerError(
                "resolve pool exhausted on subscription_expired account; "
                "requeueing for sherlock-capable account"
            )

        log.info(
            "nick_search.telegram.fallback_sherlock_bot",
            account_id=ctx.account.id,
            nick=inp.nick,
        )

    if inp.search_in == "vk":
        payload_text = _normalize_vk_profile_url(inp.nick)
        choice_button_text = None
    else:
        payload_text = inp.nick
        choice_button_text = {
            "instagram": "Instagram",
            "tiktok": "Tiktok",
            "telegram": "Telegram",
        }[inp.search_in]
    outcome = await _run_and_pack(
        ctx=ctx,
        payload_text=payload_text,
        choice_button_text=choice_button_text,
        max_pages=inp.max_pages,
    )
    if inp.search_in == "telegram":
        outcome.data["resolve_method"] = "sherlock_bot"
        outcome.meta["resolve_method"] = "sherlock_bot"
    return outcome


@register_handler("photo_search")
async def photo_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: PhotoSearchInput = validate_input(
        "photo_search", ctx.task.input
    )
    photo_path = Path(inp.photo_path).expanduser()
    if not photo_path.is_absolute():
        settings = get_settings()
        root = Path(getattr(settings, "storage_dir", "storage")).resolve()
        photo_path = (root / photo_path).resolve()
    if not photo_path.exists():
        raise HandlerPermanentError(f"photo not found: {photo_path}")

    return await _run_and_pack(
        ctx=ctx,
        payload_photo=photo_path,
        max_pages=inp.max_pages,
    )
