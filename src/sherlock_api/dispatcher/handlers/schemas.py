from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sherlock_api.dispatcher.handlers.base import HandlerPermanentError

_PHONE_CLEAN_RE = re.compile(r"[^\d]")


class _ScenarioInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    scenario: ClassVar[str]


class PhoneSearchInput(_ScenarioInput):
    scenario: ClassVar[str] = "phone_search"

    phone: str = Field(
        ...,
        description=("Телефон для поиска. Плюс, пробелы, скобки и дефисы будут удалены."),
        examples=["+79116123118", "79116123118", "+7 911 612 31 18"],
    )
    max_pages: int = Field(
        default=1,
        ge=1,
        le=50,
        description=("Максимум страниц для сбора."),
    )

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        raw = (v or "").strip()
        digits = _PHONE_CLEAN_RE.sub("", raw)
        if not (7 <= len(digits) <= 15):
            raise ValueError(
                f"phone must have 7..15 digits after normalization, got {len(digits)} in {raw!r}"
            )
        return f"+{digits}" if raw.startswith("+") else digits


class NickSearchInput(_ScenarioInput):
    scenario: ClassVar[str] = "nick_search"

    nick: str = Field(
        ...,
        min_length=2,
        max_length=128,
        description=("Ник, username Telegram или числовой id."),
        examples=["@raizzep", "raizzep", "5136214812"],
    )
    max_pages: int = Field(default=1, ge=1, le=50)
    search_in: Literal["instagram", "tiktok", "telegram"] = Field(
        default="telegram",
        description=("Где продолжить поиск после выбора источника."),
    )

    @field_validator("nick")
    @classmethod
    def _clean_nick(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("nick must not be empty")
        return s


class PhotoSearchInput(_ScenarioInput):
    scenario: ClassVar[str] = "photo_search"

    photo_path: str = Field(
        ...,
        description=("Путь до файла фото на сервере."),
    )
    max_pages: int = Field(
        default=20,
        ge=1,
        le=100,
        description=("Максимум страниц для выдачи по фото."),
    )


class _SimpleReportInput(_ScenarioInput):
    pass


def _digits(s: str) -> str:
    return _PHONE_CLEAN_RE.sub("", s or "")


class EmailSearchInput(_SimpleReportInput):
    scenario: ClassVar[str] = "email_search"

    email: str = Field(
        ...,
        min_length=3,
        max_length=255,
        description="Email для поиска.",
        examples=["foo@bar.com"],
    )

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        s = (v or "").strip()
        if "@" not in s:
            raise ValueError("email must contain '@'")
        return s


class CarPlateSearchInput(_SimpleReportInput):
    scenario: ClassVar[str] = "car_plate_search"

    plate: str = Field(
        ...,
        min_length=4,
        max_length=15,
        description="Госномер автомобиля.",
        examples=["О940СА178", "А123ВС777"],
    )

    @field_validator("plate")
    @classmethod
    def _normalize_plate(cls, v: str) -> str:
        s = (v or "").strip().upper().replace(" ", "")
        if not s:
            raise ValueError("plate must not be empty")
        return s


class VinSearchInput(_SimpleReportInput):
    scenario: ClassVar[str] = "vin_search"

    vin: str = Field(
        ...,
        min_length=11,
        max_length=20,
        description="VIN автомобиля.",
        examples=["XTA211440C5106924"],
    )

    @field_validator("vin")
    @classmethod
    def _normalize_vin(cls, v: str) -> str:
        s = (v or "").strip().upper().replace(" ", "")
        if not s:
            raise ValueError("vin must not be empty")
        return s


class AddressSearchInput(_SimpleReportInput):
    scenario: ClassVar[str] = "address_search"

    address: str = Field(
        ...,
        min_length=4,
        max_length=512,
        description=("Адрес одной строкой."),
        examples=["Город Москва ул. Правды 1, кв 1"],
    )

    @field_validator("address")
    @classmethod
    def _trim(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("address must not be empty")
        return s


class CadastreSearchInput(_SimpleReportInput):
    scenario: ClassVar[str] = "cadastre_search"

    cadastre: str = Field(
        ...,
        min_length=8,
        max_length=64,
        description="Кадастровый номер.",
        examples=["77:01:0004042:6987"],
    )

    @field_validator("cadastre")
    @classmethod
    def _trim(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("cadastre must not be empty")
        return s


class _DocsInput(_SimpleReportInput):
    @staticmethod
    def _digits(v: str) -> str:
        return _digits(v)


class DocsInnSearchInput(_DocsInput):
    scenario: ClassVar[str] = "docs_inn"

    inn: str = Field(
        ...,
        description="Personal INN (12 digits).",
        examples=["123456789012"],
    )

    @field_validator("inn")
    @classmethod
    def _check(cls, v: str) -> str:
        d = _digits(v)
        if len(d) != 12:
            raise ValueError(f"personal INN must be 12 digits, got {len(d)}")
        return d


class DocsSnilsSearchInput(_DocsInput):
    scenario: ClassVar[str] = "docs_snils"

    snils: str = Field(
        ...,
        description="SNILS (11 digits, hyphens/spaces stripped).",
        examples=["12345678901"],
    )

    @field_validator("snils")
    @classmethod
    def _check(cls, v: str) -> str:
        d = _digits(v)
        if len(d) != 11:
            raise ValueError(f"SNILS must be 11 digits, got {len(d)}")
        return d


class DocsPassportSearchInput(_DocsInput):
    scenario: ClassVar[str] = "docs_passport"

    passport: str = Field(
        ...,
        description="Russian passport series+number (10 digits).",
        examples=["1234567890"],
    )

    @field_validator("passport")
    @classmethod
    def _check(cls, v: str) -> str:
        d = _digits(v)
        if len(d) != 10:
            raise ValueError(f"passport must be 10 digits, got {len(d)}")
        return d


class LegalSearchInput(_ScenarioInput):
    scenario: ClassVar[str] = "legal_search"

    query: str = Field(
        ...,
        description=("ИНН (10/12) или ОГРН/ОГРНИП (13/15)."),
        examples=["2540214547", "1107449004464"],
    )

    @field_validator("query")
    @classmethod
    def _check(cls, v: str) -> str:
        d = _digits(v)
        if len(d) not in (10, 12, 13, 15):
            raise ValueError("query must be 10/12 digits (INN) or 13/15 digits (OGRN/OGRNIP)")
        return d


class DomainIpSearchInput(_ScenarioInput):
    scenario: ClassVar[str] = "domain_ip_search"

    target: str = Field(
        ...,
        min_length=4,
        max_length=255,
        description="Домен или IP.",
        examples=["google.com", "8.8.8.8"],
    )

    @field_validator("target")
    @classmethod
    def _trim(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if not s:
            raise ValueError("target must not be empty")
        return s


_TAG_COUNTRY_CODES = {
    "all",
    "ru",
    "kz",
    "by",
    "kg",
    "ua",
    "uz",
    "79",
    "77",
    "375",
    "996",
    "380",
    "998",
}


class TagSearchInput(_ScenarioInput):
    scenario: ClassVar[str] = "tag_search"

    name: str = Field(
        ...,
        min_length=2,
        max_length=128,
        description=("Фрагмент имени для /tag."),
        examples=["хирург москва"],
    )
    country: str = Field(
        default="all",
        description=("Страна: all/ru/kz/by/kg/ua/uz или 79/77/375/996/380/998."),
    )

    @field_validator("country")
    @classmethod
    def _check_country(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if s not in _TAG_COUNTRY_CODES:
            raise ValueError(f"country must be one of {sorted(_TAG_COUNTRY_CODES)}")
        return s


SCENARIO_SCHEMAS: dict[str, type[_ScenarioInput]] = {
    cls.scenario: cls
    for cls in (
        PhoneSearchInput,
        NickSearchInput,
        PhotoSearchInput,
        EmailSearchInput,
        CarPlateSearchInput,
        VinSearchInput,
        AddressSearchInput,
        CadastreSearchInput,
        DocsInnSearchInput,
        DocsSnilsSearchInput,
        DocsPassportSearchInput,
        LegalSearchInput,
        DomainIpSearchInput,
        TagSearchInput,
    )
}


def validate_input(scenario: str, payload: dict[str, Any]) -> _ScenarioInput:
    cls = SCENARIO_SCHEMAS.get(scenario)
    if cls is None:
        raise HandlerPermanentError(f"no input schema registered for scenario {scenario!r}")
    try:
        return cls.model_validate(payload)
    except Exception as e:
        raise HandlerPermanentError(f"invalid input for {scenario!r}: {e}") from e
