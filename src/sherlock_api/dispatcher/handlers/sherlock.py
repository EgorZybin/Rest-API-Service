from __future__ import annotations

from pathlib import Path
from typing import Any

from sherlock_api.config import get_settings
from sherlock_api.dispatcher.handlers._bootstrap import ensure_bootstrapped
from sherlock_api.dispatcher.handlers._flow import FlowResult, run_query_flow
from sherlock_api.dispatcher.handlers.base import (
    HandlerContext,
    HandlerOutcome,
    HandlerPermanentError,
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

log = get_logger(__name__)


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


@register_handler("nick_search")
async def nick_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: NickSearchInput = validate_input(
        "nick_search", ctx.task.input
    )
    button_by_source = {
        "instagram": "Instagram",
        "tiktok": "Tiktok",
        "telegram": "Telegram",
    }
    return await _run_and_pack(
        ctx=ctx,
        payload_text=inp.nick,
        choice_button_text=button_by_source[inp.search_in],
        max_pages=inp.max_pages,
    )


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
