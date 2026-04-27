from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NAME_RE = re.compile(r"🏢\s*(.+?)(?:\n|$)")
_ACTIVITY_RE = re.compile(r"🏢[^\n]*\n([^\n]+)\n", re.MULTILINE)

_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "inn": re.compile(r"^ИНН:\s*(.+?)$", re.MULTILINE),
    "ogrn": re.compile(r"^ОГРН(?:ИП)?:\s*(.+?)$", re.MULTILINE),
    "registered_at": re.compile(r"^Дата регистрации:\s*(.+?)$", re.MULTILINE),
    "status": re.compile(r"^Статус:\s*(.+?)$", re.MULTILINE),
    "director": re.compile(r"^Директор:\s*(.+?)$", re.MULTILINE),
    "employees": re.compile(r"^Сотрудников:\s*(.+?)$", re.MULTILINE),
    "address": re.compile(r"^Адрес:\s*(.+?)$", re.MULTILINE),
}

_FIN_RE = re.compile(r"^([А-ЯЁA-Z][^:\n]+?):\s*([\d\s.,]+)\s*(?:₽|руб)", re.MULTILINE)
_FOUNDERS_BLOCK_RE = re.compile(
    r"📝\s*Учредители:\s*\n((?:[^\n]+\n?)+?)(?:\n\s*\n|\n👁|\Z)",
    re.MULTILINE,
)

_INTEREST_RE = re.compile(r"Интересовались\s+этим\s*:\s*([\d\s.,]+)")


def _parse_int(raw: str | None) -> int | None:
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


@dataclass(slots=True)
class LegalEntity:
    name: str | None
    activity: str | None
    inn: str | None
    ogrn: str | None
    registered_at: str | None
    status: str | None
    director: str | None
    director_inn: str | None
    director_callback: str | None
    employees: int | None
    address: str | None
    finances: dict[str, int] = field(default_factory=dict)
    founders: list[str] = field(default_factory=list)
    interest: int | None = None
    reviews_url: str | None = None
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "activity": self.activity,
            "inn": self.inn,
            "ogrn": self.ogrn,
            "registered_at": self.registered_at,
            "status": self.status,
            "director": self.director,
            "director_inn": self.director_inn,
            "director_callback": self.director_callback,
            "employees": self.employees,
            "address": self.address,
            "finances": self.finances,
            "founders": self.founders,
            "interest": self.interest,
            "reviews_url": self.reviews_url,
        }


@dataclass(slots=True)
class PersonSummary:
    full_name: str | None
    birthday: str | None
    age: int | None
    phone: str | None
    email: str | None
    inn: str | None
    snils: str | None
    passport: str | None
    foreign_passport: str | None
    driver_license: str | None
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "birthday": self.birthday,
            "age": self.age,
            "phone": self.phone,
            "email": self.email,
            "inn": self.inn,
            "snils": self.snils,
            "passport": self.passport,
            "foreign_passport": self.foreign_passport,
            "driver_license": self.driver_license,
        }


def _capture(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _extract_director_inn(director_line: str | None) -> str | None:
    if not director_line:
        return None
    m = re.search(r"ИНН\s+(\d+)", director_line)
    return m.group(1) if m else None


def _find_director_callback(
    buttons: list[list[dict[str, Any]]] | None,
) -> tuple[str | None, str | None]:
    if not buttons:
        return None, None
    for row in buttons:
        for btn in row:
            data = btn.get("data_utf8") or ""
            if data.startswith("summary/inn/"):
                return data, btn.get("text")
    return None, None


def _find_reviews_url(
    buttons: list[list[dict[str, Any]]] | None,
) -> str | None:
    if not buttons:
        return None
    for row in buttons:
        for btn in row:
            text = btn.get("text") or ""
            url = btn.get("url")
            if url and ("Комментарии" in text or "Отзывы" in text):
                return url
    return None


def parse_legal_card(
    text: str,
    *,
    buttons: list[list[dict[str, Any]]] | None = None,
) -> LegalEntity:
    text = text or ""
    name = _capture(_NAME_RE, text)
    activity_match = _ACTIVITY_RE.search(text)
    activity = activity_match.group(1).strip() if activity_match else None
    fields: dict[str, str | None] = {k: _capture(rx, text) for k, rx in _FIELD_PATTERNS.items()}
    finances: dict[str, int] = {}
    for label, raw in _FIN_RE.findall(text):
        key = label.strip().lower().replace(" ", "_")
        val = _parse_int(raw)
        if val is not None:
            finances[key] = val
    founders: list[str] = []
    fb = _FOUNDERS_BLOCK_RE.search(text)
    if fb:
        for line in fb.group(1).splitlines():
            s = line.strip()
            if s:
                founders.append(s)
    interest_m = _INTEREST_RE.search(text)
    cb_data, cb_text = _find_director_callback(buttons)
    director_value = fields.get("director") or cb_text
    return LegalEntity(
        name=name,
        activity=activity,
        inn=fields.get("inn"),
        ogrn=fields.get("ogrn"),
        registered_at=fields.get("registered_at"),
        status=fields.get("status"),
        director=director_value,
        director_inn=_extract_director_inn(fields.get("director")),
        director_callback=cb_data,
        employees=_parse_int(fields.get("employees")),
        address=fields.get("address"),
        finances=finances,
        founders=founders,
        interest=_parse_int(interest_m.group(1) if interest_m else None),
        reviews_url=_find_reviews_url(buttons),
        raw_text=text,
    )


_PERSON_FIELDS = {
    "full_name": re.compile(r"^ФИО:\s*(.+?)$", re.MULTILINE),
    "birthday": re.compile(r"^Дата рождения:\s*(.+?)$", re.MULTILINE),
    "phone": re.compile(r"^Телефон:\s*(.+?)$", re.MULTILINE),
    "email": re.compile(r"^Email:\s*(.+?)$", re.MULTILINE),
    "inn": re.compile(r"^ИНН:\s*(.+?)$", re.MULTILINE),
    "snils": re.compile(r"^СНИЛС:\s*(.+?)$", re.MULTILINE),
    "passport": re.compile(r"^Паспорт:\s*(.+?)$", re.MULTILINE),
    "foreign_passport": re.compile(r"^Загранпаспорт:\s*(.+?)$", re.MULTILINE),
    "driver_license": re.compile(r"^Вод\. удостоверение:\s*(.+?)$", re.MULTILINE),
}
_AGE_RE = re.compile(r"Возраст:\s*(\d+)")


def parse_person_summary(text: str) -> PersonSummary:
    text = text or ""
    raw: dict[str, str | None] = {k: _capture(rx, text) for k, rx in _PERSON_FIELDS.items()}
    bd_raw = raw.get("birthday")
    bd_clean: str | None = None
    age: int | None = None
    if bd_raw:
        date_part = bd_raw.split("(")[0].strip()
        bd_clean = date_part or bd_raw.strip()
        age_match = _AGE_RE.search(bd_raw)
        if age_match:
            age = int(age_match.group(1))
    return PersonSummary(
        full_name=raw.get("full_name"),
        birthday=bd_clean,
        age=age,
        phone=raw.get("phone"),
        email=raw.get("email"),
        inn=raw.get("inn"),
        snils=raw.get("snils"),
        passport=raw.get("passport"),
        foreign_passport=raw.get("foreign_passport"),
        driver_license=raw.get("driver_license"),
        raw_text=text,
    )


__all__ = [
    "LegalEntity",
    "PersonSummary",
    "parse_legal_card",
    "parse_person_summary",
]
