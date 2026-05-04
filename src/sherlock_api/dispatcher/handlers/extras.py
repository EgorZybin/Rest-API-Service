from __future__ import annotations

from typing import Any

from telethon.errors import (
    BotResponseTimeoutError,
    FloodWaitError,
)
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

from sherlock_api.dispatcher.handlers._bootstrap import ensure_bootstrapped
from sherlock_api.dispatcher.handlers._flow import (
    FlowResult,
    RawBotMessage,
    _await_next_page,
    _Collector,
    _reject_paywall,
    _reject_rate_limit,
    run_query_flow,
)
from sherlock_api.dispatcher.handlers.base import (
    HandlerContext,
    HandlerError,
    HandlerOutcome,
    HandlerPermanentError,
    HandlerRateLimitError,
    register_handler,
)
from sherlock_api.dispatcher.handlers.report_txt_fetch import merge_report_txt_if_present
from sherlock_api.dispatcher.handlers.schemas import (
    AddressSearchInput,
    CadastreSearchInput,
    CarPlateSearchInput,
    DocsInnSearchInput,
    DocsPassportSearchInput,
    DocsSnilsSearchInput,
    DomainIpSearchInput,
    EmailSearchInput,
    LegalSearchInput,
    TagSearchInput,
    VinSearchInput,
    validate_input,
)
from sherlock_api.logging import get_logger
from sherlock_api.parsers import (
    parse_domain_ip,
    parse_legal_card,
    parse_person_summary,
    parse_simple_report,
    parse_tag_page,
)

log = get_logger(__name__)


class UnexpectedSimpleReportStateError(HandlerError):
    code = "unexpected_bot_state"


_ONBOARDING_TEXT_TOKENS = (
    "добро пожаловать, агент",
    "добро пожаловать",
    "показать меню",
)


def _is_invalid_simple_report_payload(
    *,
    raw_text: str,
    query: str | None,
    matches: int | None,
    interest: int | None,
    report_url: str | None,
    reviews_url: str | None,
) -> bool:
    text = (raw_text or "").strip().lower()
    if text and any(tok in text for tok in _ONBOARDING_TEXT_TOKENS):
        return True
    return all(v is None for v in (query, matches, interest, report_url, reviews_url))


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


async def _ensure_bootstrap(ctx: HandlerContext, bot_id: int) -> bool:
    did = await ensure_bootstrapped(client=ctx.client, bot_id=bot_id, account=ctx.account)
    if did:
        log.info(
            "extras.bootstrap.first_time",
            account_id=ctx.account.id,
            bot_id=bot_id,
        )
    return did


async def _send_and_collect(
    *,
    ctx: HandlerContext,
    payload_text: str,
    max_pages: int = 1,
    pagination_button_text: str = "Показать еще",
    choice_button_text: str | None = None,
) -> tuple[FlowResult, int, bool]:
    bot_id = await _resolve_bot_id(ctx)
    did_bs = await _ensure_bootstrap(ctx, bot_id)
    flow = await run_query_flow(
        client=ctx.client,
        bot_id=bot_id,
        payload_text=payload_text,
        choice_button_text=choice_button_text,
        max_pages=max_pages,
        pagination_button_text=pagination_button_text,
        sink=ctx.audit,
    )
    if flow.last_bot_message_at is not None:
        ctx.account.last_bot_ack_at = flow.last_bot_message_at
    return flow, bot_id, did_bs


def _ensure_first_page(flow: FlowResult) -> RawBotMessage:
    if not flow.pages:
        raise HandlerError("bot did not produce a usable result page")
    return flow.pages[0]


async def _simple_report_outcome(
    *,
    ctx: HandlerContext,
    payload_text: str,
) -> HandlerOutcome:
    flow, _bot_id, did_bs = await _send_and_collect(ctx=ctx, payload_text=payload_text, max_pages=1)
    page = _ensure_first_page(flow)
    parsed = parse_simple_report(page.text, buttons=page.buttons)
    if _is_invalid_simple_report_payload(
        raw_text=parsed.raw_text,
        query=parsed.query,
        matches=parsed.matches,
        interest=parsed.interest,
        report_url=parsed.report_url,
        reviews_url=parsed.reviews_url,
    ):
        raise UnexpectedSimpleReportStateError(
            "bot returned onboarding/empty response instead of a report"
        )
    data: dict[str, Any] = {
        **parsed.to_dict(),
        "raw_text": parsed.raw_text,
        "account_id": ctx.account.id,
        "bootstrap_performed": did_bs,
    }
    await merge_report_txt_if_present(data)
    return HandlerOutcome(
        data=data,
        meta={
            "matches": parsed.matches,
            "has_report_url": parsed.report_url is not None,
        },
    )


@register_handler("email_search")
async def email_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: EmailSearchInput = validate_input(
        "email_search", ctx.task.input
    )
    return await _simple_report_outcome(ctx=ctx, payload_text=inp.email)


@register_handler("car_plate_search")
async def car_plate_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: CarPlateSearchInput = validate_input(
        "car_plate_search", ctx.task.input
    )
    return await _simple_report_outcome(ctx=ctx, payload_text=inp.plate)


@register_handler("vin_search")
async def vin_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: VinSearchInput = validate_input(
        "vin_search", ctx.task.input
    )
    return await _simple_report_outcome(ctx=ctx, payload_text=inp.vin)


@register_handler("address_search")
async def address_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: AddressSearchInput = validate_input(
        "address_search", ctx.task.input
    )
    return await _simple_report_outcome(ctx=ctx, payload_text=f"/adr {inp.address}")


@register_handler("cadastre_search")
async def cadastre_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: CadastreSearchInput = validate_input(
        "cadastre_search", ctx.task.input
    )
    return await _simple_report_outcome(ctx=ctx, payload_text=inp.cadastre)


@register_handler("docs_inn")
async def docs_inn_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: DocsInnSearchInput = validate_input(
        "docs_inn", ctx.task.input
    )
    return await _simple_report_outcome(ctx=ctx, payload_text=f"/inn {inp.inn}")


@register_handler("docs_snils")
async def docs_snils_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: DocsSnilsSearchInput = validate_input(
        "docs_snils", ctx.task.input
    )
    return await _simple_report_outcome(ctx=ctx, payload_text=f"/snils {inp.snils}")


@register_handler("docs_passport")
async def docs_passport_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: DocsPassportSearchInput = validate_input(
        "docs_passport", ctx.task.input
    )
    return await _simple_report_outcome(ctx=ctx, payload_text=f"/passport {inp.passport}")


@register_handler("domain_ip_search")
async def domain_ip_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: DomainIpSearchInput = validate_input(
        "domain_ip_search", ctx.task.input
    )
    flow, _bot_id, did_bs = await _send_and_collect(ctx=ctx, payload_text=inp.target, max_pages=1)
    page = _ensure_first_page(flow)
    parsed = parse_domain_ip(page.text)
    data: dict[str, Any] = {
        **parsed.to_dict(),
        "raw_text": parsed.raw_text,
        "account_id": ctx.account.id,
        "bootstrap_performed": did_bs,
    }
    return HandlerOutcome(data=data, meta={"has_ip": parsed.ip is not None})


def _looks_like_inn(query: str) -> bool:
    return len(query) in (10, 12)


async def _click_callback_and_wait(
    *,
    ctx: HandlerContext,
    bot_id: int,
    msg_id: int,
    callback_data: bytes,
    button_text: str | None,
    timeout: float = 30.0,
) -> RawBotMessage | None:
    collector = _Collector(client=ctx.client, bot_id=bot_id)
    collector.attach()
    try:
        try:
            await ctx.client(
                GetBotCallbackAnswerRequest(peer=bot_id, msg_id=msg_id, data=callback_data)
            )
            ctx.audit(
                {
                    "direction": "out",
                    "kind": "button_click",
                    "msg_id": msg_id,
                    "button_text": button_text,
                }
            )
        except FloodWaitError as e:
            raise HandlerRateLimitError(f"FloodWait {e.seconds}s on legal director-click") from e
        except BotResponseTimeoutError:
            log.warning(
                "extras.click_timeout_no_answer",
                msg_id=msg_id,
                button_text=button_text,
            )
        except Exception as e:
            log.warning(
                "extras.click_failed_transient",
                msg_id=msg_id,
                button_text=button_text,
                err=repr(e),
            )

        page = await _await_next_page(
            collector=collector,
            per_page_timeout=timeout,
            loading_grace=2.0,
            album_window=1.0,
            media_dir=None,
            min_msg_id=msg_id,
        )
        if page is None:
            return None
        _reject_paywall(page)
        _reject_rate_limit(page)
        return page
    finally:
        collector.detach()


@register_handler("legal_search")
async def legal_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: LegalSearchInput = validate_input(
        "legal_search", ctx.task.input
    )
    payload = f"/inn {inp.query}" if _looks_like_inn(inp.query) else inp.query

    bot_id = await _resolve_bot_id(ctx)
    did_bs = await _ensure_bootstrap(ctx, bot_id)
    flow = await run_query_flow(
        client=ctx.client,
        bot_id=bot_id,
        payload_text=payload,
        max_pages=1,
        sink=ctx.audit,
    )
    if flow.last_bot_message_at is not None:
        ctx.account.last_bot_ack_at = flow.last_bot_message_at

    page = _ensure_first_page(flow)
    legal = parse_legal_card(page.text, buttons=page.buttons)

    data: dict[str, Any] = {
        **legal.to_dict(),
        "raw_text": legal.raw_text,
        "account_id": ctx.account.id,
        "bootstrap_performed": did_bs,
    }

    if legal.director_callback:
        followup = await _click_callback_and_wait(
            ctx=ctx,
            bot_id=bot_id,
            msg_id=page.message_id,
            callback_data=legal.director_callback.encode("utf-8"),
            button_text=legal.director,
        )
        if followup is not None:
            person = parse_person_summary(followup.text)
            data["director_summary"] = {
                **person.to_dict(),
                "raw_text": person.raw_text,
            }
        else:
            data["director_summary"] = None
    else:
        data["director_summary"] = None

    return HandlerOutcome(
        data=data,
        meta={
            "found": legal.inn is not None,
            "has_director_summary": data.get("director_summary") is not None,
        },
    )


_TAG_CODE_TO_BUTTON_TEXT: dict[str, str] = {
    "all": "Искать везде",
    "ru": "Россия",
    "79": "Россия",
    "kz": "Казахстан",
    "77": "Казахстан",
    "by": "Беларусь",
    "375": "Беларусь",
    "kg": "Кыргызстан",
    "996": "Кыргызстан",
    "ua": "Украина",
    "380": "Украина",
    "uz": "Узбекистан",
    "998": "Узбекистан",
}


def _resolve_tag_button_text(country: str) -> str:
    return _TAG_CODE_TO_BUTTON_TEXT.get(country.lower(), "Искать везде")


_TAG_MAX_PAGES_GUARD = 1000


@register_handler("tag_search")
async def tag_search_handler(ctx: HandlerContext) -> HandlerOutcome:
    inp: TagSearchInput = validate_input(
        "tag_search", ctx.task.input
    )
    button_text = _resolve_tag_button_text(inp.country)
    bot_id = await _resolve_bot_id(ctx)
    did_bs = await _ensure_bootstrap(ctx, bot_id)

    flow = await run_query_flow(
        client=ctx.client,
        bot_id=bot_id,
        payload_text=f"/tag {inp.name}",
        choice_button_text=button_text,
        max_pages=_TAG_MAX_PAGES_GUARD,
        pagination_button_text=">",
        sink=ctx.audit,
    )
    if flow.last_bot_message_at is not None:
        ctx.account.last_bot_ack_at = flow.last_bot_message_at

    if not flow.pages:
        raise HandlerError("tag_search: bot produced no result")

    parsed_pages = [parse_tag_page(p.text, buttons=p.buttons) for p in flow.pages]
    total_matches = next(
        (pp.total_matches for pp in parsed_pages if pp.total_matches is not None),
        None,
    )
    pages_total = next((pp.pages for pp in parsed_pages if pp.pages is not None), None)
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    for pp in parsed_pages:
        for r in pp.records:
            if r.index in seen:
                continue
            seen.add(r.index)
            records.append(r.to_dict())

    data: dict[str, Any] = {
        "query": inp.name,
        "country": inp.country,
        "country_button": button_text,
        "total_matches": total_matches,
        "pages_total": pages_total,
        "pages_collected": len(parsed_pages),
        "records": records,
        "account_id": ctx.account.id,
        "bootstrap_performed": did_bs,
    }
    return HandlerOutcome(
        data=data,
        meta={
            "records": len(records),
            "pages_collected": len(parsed_pages),
        },
    )


__all__ = [
    "address_search_handler",
    "cadastre_search_handler",
    "car_plate_search_handler",
    "docs_inn_handler",
    "docs_passport_handler",
    "docs_snils_handler",
    "domain_ip_search_handler",
    "email_search_handler",
    "legal_search_handler",
    "tag_search_handler",
    "vin_search_handler",
]
