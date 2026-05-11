from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from telethon import TelegramClient, events
from telethon.errors import (
    BotResponseTimeoutError,
    ChatWriteForbiddenError,
    FloodWaitError,
    UserBannedInChannelError,
)
from telethon.tl.custom import Message
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.tl.types import (
    KeyboardButtonCallback,
    ReplyInlineMarkup,
)

from sherlock_api.dispatcher.handlers.base import (
    HandlerError,
    HandlerPermanentError,
    HandlerRateLimitError,
    HandlerSubscriptionError,
)
from sherlock_api.logging import get_logger
from sherlock_api.parsers import (
    ParsedPage,
    SherlockResult,
    is_loading_message,
    is_paywall_message,
    parse_result_message,
)

log = get_logger(__name__)


class AuditSink(Protocol):
    def __call__(self, event: dict[str, Any]) -> None:
        ...


def _noop_sink(_event: dict[str, Any]) -> None:
    pass


@dataclass(slots=True)
class RawBotMessage:
    message_id: int
    text: str
    buttons: list[list[dict[str, Any]]] | None
    entities: list[dict[str, Any]] | None
    grouped_id: int | None
    media_path: str | None
    parsed: ParsedPage

    @property
    def is_loading(self) -> bool:
        return self.parsed.is_loading

    @property
    def is_paywall(self) -> bool:
        return self.parsed.is_paywall


@dataclass
class _Collector:
    client: TelegramClient
    bot_id: int
    our_sent_id: int | None = None

    messages: dict[int, Message] = field(default_factory=dict)
    bump: asyncio.Event = field(default_factory=asyncio.Event)

    _new_handler: Any = None
    _edit_handler: Any = None

    def attach(self) -> None:
        @events.register(events.NewMessage(from_users=self.bot_id))
        async def _on_new(event: events.NewMessage.Event) -> None:
            self._record(event.message)

        @events.register(events.MessageEdited(from_users=self.bot_id))
        async def _on_edit(event: events.MessageEdited.Event) -> None:
            self._record(event.message)

        self.client.add_event_handler(_on_new)
        self.client.add_event_handler(_on_edit)
        self._new_handler = _on_new
        self._edit_handler = _on_edit

    def detach(self) -> None:
        for h in (self._new_handler, self._edit_handler):
            if h is None:
                continue
            try:
                self.client.remove_event_handler(h)
            except Exception:
                pass

    def _record(self, msg: Message) -> None:
        self.messages[msg.id] = msg
        self.bump.set()

    async def wait_for_change(self, timeout: float) -> bool:
        self.bump.clear()
        try:
            await asyncio.wait_for(self.bump.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


def _serialize_buttons(markup: Any) -> list[list[dict[str, Any]]] | None:
    if not isinstance(markup, ReplyInlineMarkup):
        return None
    out: list[list[dict[str, Any]]] = []
    for row in markup.rows:
        r: list[dict[str, Any]] = []
        for btn in row.buttons:
            item: dict[str, Any] = {
                "type": type(btn).__name__,
                "text": getattr(btn, "text", None),
            }
            if isinstance(btn, KeyboardButtonCallback):
                data: bytes = btn.data or b""
                item["data_hex"] = data.hex()
                try:
                    item["data_utf8"] = data.decode("utf-8")
                except UnicodeDecodeError:
                    item["data_utf8"] = None
            else:
                url = getattr(btn, "url", None)
                if url is not None:
                    item["url"] = url
            r.append(item)
        out.append(r)
    return out


def _serialize_entities(entities: Any) -> list[dict[str, Any]] | None:
    if not entities:
        return None
    out: list[dict[str, Any]] = []
    for e in entities:
        d: dict[str, Any] = {"type": type(e).__name__}
        for attr in ("offset", "length", "url"):
            if hasattr(e, attr):
                d[attr] = getattr(e, attr)
        out.append(d)
    return out


def _next_callback(
    buttons: list[list[dict[str, Any]]] | None,
    *,
    button_text: str = "Показать еще",
) -> bytes | None:
    if not buttons:
        return None
    want = (button_text or "").strip().lower()
    aliases = {
        want,
        "показать еще",
        "показать ещё",
        "далее",
        "следующая",
        "следующая страница",
        "next",
        ">",
        ">>",
        "›",
        "❯",
        "❱",
        "»",
        "→",
    }
    for row in buttons:
        for btn in row:
            text = (btn.get("text") or "").strip().lower()
            if text in aliases:
                data_utf8 = btn.get("data_utf8")
                if data_utf8:
                    return data_utf8.encode("utf-8")
                data_hex = btn.get("data_hex")
                if isinstance(data_hex, str) and data_hex:
                    try:
                        return bytes.fromhex(data_hex)
                    except ValueError:
                        continue
    return None


def _callback_by_button_text(
    buttons: list[list[dict[str, Any]]] | None, button_text: str
) -> bytes | None:
    if not buttons:
        return None
    want = (button_text or "").strip().lower()
    if not want:
        return None
    for row in buttons:
        for btn in row:
            text = str(btn.get("text") or "").strip().lower()
            if text != want:
                continue
            data_utf8 = btn.get("data_utf8")
            if data_utf8:
                return data_utf8.encode("utf-8")
            data_hex = btn.get("data_hex")
            if isinstance(data_hex, str) and data_hex:
                try:
                    return bytes.fromhex(data_hex)
                except ValueError:
                    continue
    return None


def _button_texts(buttons: list[list[dict[str, Any]]] | None) -> list[str]:
    if not buttons:
        return []
    out: list[str] = []
    for row in buttons:
        for btn in row:
            text = str(btn.get("text") or "").strip()
            if text:
                out.append(text)
    return out


async def _download_media(msg: Message, *, into_dir: Path) -> str | None:
    if not msg.media:
        return None
    into_dir.mkdir(parents=True, exist_ok=True)
    try:
        path = await msg.download_media(file=str(into_dir / f"{msg.id}"))
        return str(path) if path else None
    except Exception as e:
        log.warning("flow.media_download_failed", msg_id=msg.id, err=repr(e))
        return None


def _to_raw(msg: Message, *, media_path: str | None) -> RawBotMessage:
    text = msg.message or ""
    buttons = _serialize_buttons(getattr(msg, "reply_markup", None))
    entities = _serialize_entities(getattr(msg, "entities", None))
    parsed = parse_result_message(text, buttons=buttons, entities=entities, media_path=media_path)
    return RawBotMessage(
        message_id=msg.id,
        text=text,
        buttons=buttons,
        entities=entities,
        grouped_id=getattr(msg, "grouped_id", None),
        media_path=media_path,
        parsed=parsed,
    )


@dataclass(slots=True)
class FlowResult:
    pages: list[RawBotMessage]
    last_bot_message_at: datetime | None

    @property
    def results(self) -> list[SherlockResult]:
        return [p.parsed.result for p in self.pages if p.parsed.result is not None]

    def to_merged(self) -> dict[str, Any]:
        results = [r.to_dict() for r in self.results]
        pagination_total = None
        for p in self.pages:
            if p.parsed.pagination and p.parsed.pagination.total:
                pagination_total = p.parsed.pagination.total
                break
        return {
            "pages_collected": len(self.pages),
            "pagination_total": pagination_total,
            "results": results,
        }


async def run_query_flow(
    *,
    client: TelegramClient,
    bot_id: int,
    payload_text: str | None = None,
    payload_photo: str | Path | None = None,
    choice_button_text: str | None = None,
    max_pages: int = 1,
    pagination_button_text: str = "Показать еще",
    per_page_timeout: float = 30.0,
    loading_grace: float = 2.0,
    album_window: float = 1.5,
    media_dir: Path | None = None,
    sink: AuditSink | None = None,
) -> FlowResult:
    if (payload_text is None) == (payload_photo is None):
        raise HandlerPermanentError(
            "run_query_flow: specify exactly one of payload_text / payload_photo"
        )
    sink = sink or _noop_sink

    collector = _Collector(client=client, bot_id=bot_id)
    collector.attach()

    pages: list[RawBotMessage] = []
    last_bot_at: datetime | None = None

    try:
        try:
            if payload_text is not None:
                sent = await client.send_message(bot_id, payload_text)
                sink(
                    {
                        "direction": "out",
                        "kind": "text",
                        "msg_id": sent.id,
                        "text": payload_text,
                    }
                )
            else:
                photo_path = Path(payload_photo)
                if not photo_path.exists():
                    raise HandlerPermanentError(f"photo does not exist: {photo_path}")
                sent = await client.send_file(bot_id, str(photo_path), force_document=False)
                sink(
                    {
                        "direction": "out",
                        "kind": "photo",
                        "msg_id": sent.id,
                        "path": str(photo_path),
                    }
                )
            collector.our_sent_id = sent.id
        except FloodWaitError as e:
            raise HandlerRateLimitError(f"FloodWait {e.seconds}s on send") from e
        except (ChatWriteForbiddenError, UserBannedInChannelError) as e:
            raise HandlerSubscriptionError(f"cannot write to bot: {type(e).__name__}") from e

        first_page = await _await_next_page(
            collector=collector,
            per_page_timeout=per_page_timeout,
            loading_grace=loading_grace,
            album_window=album_window,
            media_dir=media_dir,
        )
        if first_page is None:
            raise HandlerError(f"no response from bot within {per_page_timeout:.0f}s")

        _reject_paywall(first_page)
        _reject_rate_limit(first_page)

        if choice_button_text is not None:
            choice_cb = _callback_by_button_text(first_page.buttons, choice_button_text)
            if choice_cb is None:
                options = _button_texts(first_page.buttons)
                log.warning(
                    "flow.choice_button_missing",
                    want=choice_button_text,
                    available=options,
                    msg_id=first_page.message_id,
                )
            else:
                try:
                    await client(
                        GetBotCallbackAnswerRequest(
                            peer=bot_id,
                            msg_id=first_page.message_id,
                            data=choice_cb,
                        )
                    )
                    sink(
                        {
                            "direction": "out",
                            "kind": "button_click",
                            "msg_id": first_page.message_id,
                            "button_text": choice_button_text,
                        }
                    )
                except FloodWaitError as e:
                    raise HandlerRateLimitError(f"FloodWait {e.seconds}s on choice click") from e
                except BotResponseTimeoutError:
                    log.warning(
                        "flow.choice_click_timeout_no_answer",
                        msg_id=first_page.message_id,
                        button_text=choice_button_text,
                    )
                except Exception as e:
                    log.warning(
                        "flow.choice_click_failed_transient",
                        msg_id=first_page.message_id,
                        button_text=choice_button_text,
                        err=repr(e),
                    )

            chosen_page = await _await_next_page(
                collector=collector,
                per_page_timeout=per_page_timeout,
                loading_grace=loading_grace,
                album_window=album_window,
                media_dir=media_dir,
                expect_msg_id=first_page.message_id,
                min_msg_id=first_page.message_id,
            )
            if chosen_page is None:
                raise HandlerError(f"no response after choice step within {per_page_timeout:.0f}s")
            _reject_paywall(chosen_page)
            _reject_rate_limit(chosen_page)
            first_page = chosen_page

        pages.append(first_page)
        last_bot_at = datetime.now(timezone.utc)
        sink(
            {
                "direction": "in",
                "kind": "result_page",
                "msg_id": first_page.message_id,
                "page": 1,
                "text_prefix": first_page.text[:120],
                "available_buttons": _button_texts(first_page.buttons),
            }
        )

        last_msg_id: int | None = first_page.message_id
        last_buttons = first_page.buttons
        while len(pages) < max_pages:
            next_cb = _next_callback(last_buttons, button_text=pagination_button_text)
            if next_cb is None or last_msg_id is None:
                if last_msg_id is not None:
                    sink(
                        {
                            "direction": "meta",
                            "kind": "pagination_stop",
                            "msg_id": last_msg_id,
                            "reason": "no_next_button",
                            "available_buttons": _button_texts(last_buttons),
                        }
                    )
                break
            try:
                await client(
                    GetBotCallbackAnswerRequest(peer=bot_id, msg_id=last_msg_id, data=next_cb)
                )
            except FloodWaitError as e:
                raise HandlerRateLimitError(f"FloodWait {e.seconds}s on pagination") from e
            except BotResponseTimeoutError:
                log.debug(
                    "flow.pagination_click_timeout_no_answer",
                    msg_id=last_msg_id,
                )
            except Exception as e:
                log.warning(
                    "flow.pagination_click_failed",
                    msg_id=last_msg_id,
                    err=repr(e),
                )
                break

            nxt = await _await_next_page(
                collector=collector,
                per_page_timeout=per_page_timeout,
                loading_grace=loading_grace,
                album_window=album_window,
                media_dir=media_dir,
                expect_msg_id=last_msg_id,
                min_msg_id=last_msg_id,
            )
            if nxt is None:
                log.warning("flow.pagination_timeout", after_msg_id=last_msg_id)
                break

            _reject_paywall(nxt)
            _reject_rate_limit(nxt)
            pages.append(nxt)
            last_bot_at = datetime.now(timezone.utc)
            last_msg_id = nxt.message_id
            last_buttons = nxt.buttons
            sink(
                {
                    "direction": "in",
                    "kind": "result_page",
                    "msg_id": nxt.message_id,
                    "page": len(pages),
                    "text_prefix": nxt.text[:120],
                    "available_buttons": _button_texts(nxt.buttons),
                }
            )

        return FlowResult(pages=pages, last_bot_message_at=last_bot_at)
    finally:
        collector.detach()


async def _await_next_page(
    *,
    collector: _Collector,
    per_page_timeout: float,
    loading_grace: float,
    album_window: float,
    media_dir: Path | None,
    expect_msg_id: int | None = None,
    min_msg_id: int | None = None,
) -> RawBotMessage | None:
    deadline = asyncio.get_event_loop().time() + per_page_timeout

    def _candidate() -> Message | None:
        best: Message | None = None
        for mid, msg in collector.messages.items():
            if min_msg_id is not None and mid <= min_msg_id and mid != expect_msg_id:
                continue
            if getattr(msg, "out", False):
                continue
            if is_loading_message(msg.message or ""):
                continue
            if best is None or mid > best.id:
                best = msg
        return best

    while True:
        cand = _candidate()
        if cand is not None:
            break
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return None
        got = await collector.wait_for_change(timeout=remaining)
        if not got:
            return None

    grouped_id = getattr(cand, "grouped_id", None)
    if grouped_id is not None:
        album_deadline = asyncio.get_event_loop().time() + album_window
        while asyncio.get_event_loop().time() < album_deadline:
            got = await collector.wait_for_change(
                timeout=max(
                    0.05,
                    album_deadline - asyncio.get_event_loop().time(),
                )
            )
            if not got:
                break

    if is_loading_message(cand.message or ""):
        grace_deadline = asyncio.get_event_loop().time() + loading_grace
        while asyncio.get_event_loop().time() < grace_deadline:
            got = await collector.wait_for_change(
                timeout=max(0.05, grace_deadline - asyncio.get_event_loop().time())
            )
            if not got:
                break
            fresh = collector.messages.get(cand.id) or cand
            if not is_loading_message(fresh.message or ""):
                cand = fresh
                break

    media_path: str | None = None
    if media_dir is not None and cand.media:
        media_path = await _download_media(cand, into_dir=media_dir)

    return _to_raw(cand, media_path=media_path)


def _reject_paywall(page: RawBotMessage) -> None:
    if page.is_paywall or is_paywall_message(page.text):
        raise HandlerSubscriptionError(f"paywall message from bot: {page.text[:160]!r}")


_RATE_LIMIT_PHRASES = (
    "слишком часто",
    "подождите, прежде чем",
    "повторите попытку",
    "rate limit",
    "слишком много запросов",
)


def _reject_rate_limit(page: RawBotMessage) -> None:
    low = (page.text or "").lower()
    if any(p in low for p in _RATE_LIMIT_PHRASES):
        raise HandlerRateLimitError(f"bot-side rate limit: {page.text[:160]!r}")


__all__ = [
    "AuditSink",
    "FlowResult",
    "RawBotMessage",
    "run_query_flow",
]
