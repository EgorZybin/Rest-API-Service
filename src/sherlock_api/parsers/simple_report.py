from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_MATCHES_RE = re.compile(
    r"Обнаружен(?:о|а)\s+([\d\s.,]+)\s+совпадени(?:е|я|й)",
    re.IGNORECASE,
)
_INTEREST_RE = re.compile(
    r"Интересовались\s+этим\s*:\s*([\d\s.,]+)",
    re.IGNORECASE,
)
_QUERY_RE = re.compile(r"Запрос\s*:\s*(.+?)(?:\n|$)")

_REPORT_BUTTON_PREFIX = "Открыть полный отчет"
_REVIEWS_BUTTON_TOKENS = ("Комментарии", "Отзывы", "Reviews")


@dataclass(slots=True)
class SimpleReport:
    query: str | None
    matches: int | None
    interest: int | None
    report_url: str | None
    reviews_url: str | None
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "matches": self.matches,
            "interest": self.interest,
            "report_url": self.report_url,
            "reviews_url": self.reviews_url,
        }


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


def _find_button_url(
    buttons: list[list[dict[str, Any]]] | None,
    *,
    text_prefix: str | None = None,
    text_tokens: tuple[str, ...] | None = None,
) -> str | None:
    if not buttons:
        return None
    for row in buttons:
        for btn in row:
            text = (btn.get("text") or "").strip()
            url = btn.get("url")
            if not url:
                continue
            if text_prefix and text_prefix in text:
                return url
            if text_tokens and any(tok in text for tok in text_tokens):
                return url
    return None


def parse_simple_report(
    text: str,
    *,
    buttons: list[list[dict[str, Any]]] | None = None,
) -> SimpleReport:
    text = text or ""
    q_m = _QUERY_RE.search(text)
    n_m = _MATCHES_RE.search(text)
    i_m = _INTEREST_RE.search(text)
    return SimpleReport(
        query=(q_m.group(1).strip() if q_m else None),
        matches=_parse_int(n_m.group(1) if n_m else None),
        interest=_parse_int(i_m.group(1) if i_m else None),
        report_url=_find_button_url(buttons, text_prefix=_REPORT_BUTTON_PREFIX),
        reviews_url=_find_button_url(buttons, text_tokens=_REVIEWS_BUTTON_TOKENS),
        raw_text=text,
    )


__all__ = ["SimpleReport", "parse_simple_report"]
