from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_TOTAL_RE = re.compile(r"Обнаружено\s+([\d\s.,]+)\s+совпадени")
_RECORD_RE = re.compile(
    r"^(\d+)\.\s+(\+?\d[\d\s\-()]+)\s*\n([^\n]+)\s*\n([^\n]+?)$",
    re.MULTILINE,
)
_PAGE_LABEL_RE = re.compile(r"^(\d+)\s*/\s*(\d+)$")


def _parse_int(raw: str | None) -> int | None:
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


@dataclass(slots=True)
class TagRecord:
    index: int
    phone: str
    region: str | None
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "phone": self.phone,
            "region": self.region,
            "tags": self.tags,
        }


@dataclass(slots=True)
class TagPageSummary:
    total_matches: int | None
    page: int | None
    pages: int | None
    records: list[TagRecord] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_matches": self.total_matches,
            "page": self.page,
            "pages": self.pages,
            "records": [r.to_dict() for r in self.records],
        }


def parse_tag_page(
    text: str,
    *,
    buttons: list[list[dict[str, Any]]] | None = None,
) -> TagPageSummary:
    text = text or ""
    total_m = _TOTAL_RE.search(text)
    records: list[TagRecord] = []
    for m in _RECORD_RE.finditer(text):
        idx = int(m.group(1))
        phone = re.sub(r"[\s\-()]", "", m.group(2))
        region = m.group(3).strip() or None
        tags = [t.strip() for t in m.group(4).split(",") if t.strip()]
        records.append(TagRecord(index=idx, phone=phone, region=region, tags=tags))

    page: int | None = None
    pages: int | None = None
    if buttons:
        for row in buttons:
            for btn in row:
                lab = (btn.get("text") or "").strip()
                pm = _PAGE_LABEL_RE.match(lab)
                if pm:
                    page = int(pm.group(1))
                    pages = int(pm.group(2))
                    break
            if page is not None:
                break

    return TagPageSummary(
        total_matches=_parse_int(total_m.group(1) if total_m else None),
        page=page,
        pages=pages,
        records=records,
        raw_text=text,
    )


@dataclass(slots=True)
class TagCountryButton:
    text: str
    data: str
    country_code: str | None


_COUNTRY_BUTTON_BY_TEXT: dict[str, str] = {
    "Искать везде": "all",
    "Россия": "79",
    "Казахстан": "77",
    "Беларусь": "375",
    "Кыргызстан": "996",
    "Украина": "380",
    "Узбекистан": "998",
}


def extract_country_buttons(
    buttons: list[list[dict[str, Any]]] | None,
) -> list[TagCountryButton]:
    out: list[TagCountryButton] = []
    if not buttons:
        return out
    for row in buttons:
        for btn in row:
            data = btn.get("data_utf8") or ""
            if not data.startswith("TAG/"):
                continue
            text = (btn.get("text") or "").strip()
            cc: str | None = _COUNTRY_BUTTON_BY_TEXT.get(text)
            if cc is None:
                parts = data.split("/")
                cc = parts[2] if len(parts) >= 3 and parts[2] else "all"
            out.append(TagCountryButton(text=text, data=data, country_code=cc))
    return out


def find_country_button(
    buttons: list[list[dict[str, Any]]] | None,
    *,
    code: str,
) -> TagCountryButton | None:
    aliases = {
        "all": "all",
        "ru": "79",
        "kz": "77",
        "by": "375",
        "kg": "996",
        "ua": "380",
        "uz": "998",
    }
    norm = aliases.get(code.lower(), code)
    for btn in extract_country_buttons(buttons):
        if btn.country_code == norm:
            return btn
    return None


__all__ = [
    "TagPageSummary",
    "TagRecord",
    "TagCountryButton",
    "extract_country_buttons",
    "find_country_button",
    "parse_tag_page",
]
