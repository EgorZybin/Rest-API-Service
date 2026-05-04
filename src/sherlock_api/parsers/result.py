from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from typing import Any, Iterable

from sherlock_api.parsers.simple_report import find_profile_cta_button_url

PAGE_NOOP_CALLBACK = "noop"

_LOADING_PATTERNS = (
    "идёт поиск",
    "идет поиск",
    "ищу",
    "поиск запущен",
)

_PAYWALL_PATTERNS = (
    "нужно купить запросы",
    "нужно купить запрос",
    "купите запрос",
    "закончились запросы",
    "превышен лимит",
    "подписка истекла",
    "недостаточно средств",
    "пополните баланс",
)

_PAGINATION_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")

_TOP_LEVEL_LABELS: dict[str, str] = {
    "коллекция": "collection",
    "статус": "status",
    "совпадение": "match_percent",
    "person id": "person_id",
    "фио": "fio",
    "имя": "first_name",
    "фамилия": "last_name",
    "отчество": "middle_name",
    "телефон": "phone",
    "email": "email",
    "ссылка": "link",
    "username": "username",
    "персона": "person",
    "год рождения": "birth_year",
    "месяц рождения": "birth_month",
    "день рождения": "birth_day",
    "дата рождения": "birth_date",
    "возраст": "age",
    "оператор": "phone_operator",
    "регион": "region",
    "страна": "country",
}


@dataclass(slots=True)
class PaginationInfo:
    search_id: str | None
    page: int | None
    total: int | None
    next_cb: str | None
    prev_cb: str | None

    @property
    def has_next(self) -> bool:
        return self.next_cb is not None


@dataclass(slots=True)
class SherlockResult:
    title: str | None = None
    collection: str | None = None
    status: str | None = None
    match_percent: int | None = None
    person_id: str | None = None

    fio: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    middle_name: str | None = None
    birth_year: int | None = None
    birth_month: int | None = None
    birth_day: int | None = None
    birth_date: str | None = None
    age: str | None = None

    phone: str | None = None
    phone_operator: str | None = None
    region: str | None = None
    country: str | None = None
    email: str | None = None
    username: str | None = None
    link: str | None = None
    person: str | None = None

    website_url: str | None = None
    profile_url: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)
    raw_fields: dict[str, Any] = field(default_factory=dict)

    media_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if v in (None, {}, []):
                continue
            out[f.name] = v
        return out


@dataclass(slots=True)
class ParsedPage:
    result: SherlockResult | None
    pagination: PaginationInfo | None
    is_loading: bool = False
    is_paywall: bool = False


def is_loading_message(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(p in low for p in _LOADING_PATTERNS)


def is_paywall_message(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(p in low for p in _PAYWALL_PATTERNS)


def _first_line(text: str) -> tuple[str | None, str]:
    if not text:
        return None, ""
    head, _, rest = text.partition("\n")
    head = head.strip()
    if head.startswith("💡"):
        head = head[1:].strip()
    return (head or None), rest


def _coerce_int(v: str) -> int | None:
    v = v.strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _coerce_match_percent(v: str) -> int | None:
    m = re.search(r"(\d+)", v)
    if not m:
        return None
    return int(m.group(1))


def _coerce_value(label_key: str, value: str) -> Any:
    value = value.strip().rstrip(".")
    if label_key == "match_percent":
        return _coerce_match_percent(value)
    if label_key in {"birth_year", "birth_month", "birth_day"}:
        return _coerce_int(value)
    return value or None


_LINE_PREFIX_STRIP = "├└│─-•— 🧩🕵️🔎"


def _iter_label_lines(text: str) -> Iterable[tuple[str, str]]:
    for line in text.splitlines():
        s = line.strip().lstrip(_LINE_PREFIX_STRIP).strip()
        if not s or ":" not in s:
            continue
        label, _, value = s.partition(":")
        label = label.strip().lower().lstrip(_LINE_PREFIX_STRIP).strip()
        if not label:
            continue
        yield label, value


def _utf16_slice(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    start, end = offset * 2, (offset + length) * 2
    if start < 0 or end > len(raw) or start > end:
        raise ValueError("bad utf-16 span")
    return raw[start:end].decode("utf-16-le")


def _find_url_matching_displayed_text(
    full_text: str,
    entities: list[dict[str, Any]] | None,
    displayed: str,
) -> str | None:
    want = (displayed or "").strip()
    if not entities or not want:
        return None
    for e in entities:
        et = e.get("type")
        off, ln = e.get("offset"), e.get("length")
        if off is None or ln is None:
            continue
        try:
            frag = _utf16_slice(full_text, int(off), int(ln)).strip()
        except (ValueError, UnicodeDecodeError):
            continue
        if frag != want:
            continue
        if et == "MessageEntityTextUrl":
            return e.get("url") or None
        if et == "MessageEntityUrl":
            return frag or None
    return None


def _extract_url_for_label(
    full_text: str, value_text: str, entities: list[dict[str, Any]] | None
) -> str | None:
    return _find_url_matching_displayed_text(
        full_text, entities, (value_text or "").strip()
    )


def _label_is_site_line(label: str) -> bool:
    l = (label or "").strip().lower()
    return l == "сайт" or l.endswith(" сайт")


def parse_result_message(
    text: str,
    *,
    buttons: list[list[dict[str, Any]]] | None = None,
    entities: list[dict[str, Any]] | None = None,
    media_path: str | None = None,
) -> ParsedPage:
    if is_loading_message(text):
        return ParsedPage(result=None, pagination=None, is_loading=True)
    if is_paywall_message(text):
        return ParsedPage(result=None, pagination=None, is_paywall=True)

    pagination = extract_pagination(buttons)

    if not text:
        return ParsedPage(result=None, pagination=pagination)

    title, rest = _first_line(text)

    res = SherlockResult(title=title, media_path=media_path)

    extra_marker = "Дополнительные данные:"
    if extra_marker in rest:
        head_part, _, extra_part = rest.partition(extra_marker)
    else:
        head_part, extra_part = rest, ""

    for label, value in _iter_label_lines(head_part):
        key = _TOP_LEVEL_LABELS.get(label)
        coerced = _coerce_value(key or label, value)

        if key == "link" and isinstance(coerced, str):
            url = _extract_url_for_label(text, value, entities)
            coerced = url or coerced

        if key and hasattr(res, key):
            setattr(res, key, coerced)
        else:
            res.raw_fields[label] = coerced
            if res.website_url is None and _label_is_site_line(label) and isinstance(coerced, str):
                u = _find_url_matching_displayed_text(text, entities, coerced)
                if u:
                    res.website_url = u

    for label, value in _iter_label_lines(extra_part):
        res.extra[label] = value.strip()

    res.profile_url = find_profile_cta_button_url(buttons)

    return ParsedPage(result=res, pagination=pagination, is_loading=False)


def extract_pagination(
    buttons: list[list[dict[str, Any]]] | None,
) -> PaginationInfo | None:
    if not buttons:
        return None

    flat: list[dict[str, Any]] = [b for row in buttons for b in row]
    if not flat:
        return None

    search_id: str | None = None
    page: int | None = None
    total: int | None = None
    next_cb: str | None = None
    prev_cb: str | None = None

    for btn in flat:
        text = (btn.get("text") or "").strip()
        data = btn.get("data_utf8") or ""
        if data == PAGE_NOOP_CALLBACK:
            m = _PAGINATION_RE.match(text)
            if m:
                page = int(m.group(1))
                total = int(m.group(2))
            continue
        if text == "Показать еще":
            next_cb = data
        elif text == "Назад":
            prev_cb = data

    src_cb = next_cb or prev_cb
    if src_cb:
        parts = src_cb.split("/")
        if len(parts) >= 4:
            search_id = parts[2]

    if page is None and total is None and next_cb is None and prev_cb is None:
        return None
    return PaginationInfo(
        search_id=search_id,
        page=page,
        total=total,
        next_cb=next_cb,
        prev_cb=prev_cb,
    )
