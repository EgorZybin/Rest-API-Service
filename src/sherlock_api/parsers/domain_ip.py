from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_FIELDS: dict[str, re.Pattern[str]] = {
    "ip": re.compile(r"Информация по IP/домену:\s*(.+?)(?:\n|$)", re.IGNORECASE),
    "country": re.compile(r"^Страна:\s*(.+?)$", re.MULTILINE),
    "city": re.compile(r"^Город:\s*(.+?)$", re.MULTILINE),
    "region": re.compile(r"^Регион:\s*(.+?)$", re.MULTILINE),
    "postal_code": re.compile(r"^Индекс:\s*(.+?)$", re.MULTILINE),
    "timezone": re.compile(r"^Часовой пояс:\s*(.+?)$", re.MULTILINE),
    "local_time": re.compile(r"^Местное время:\s*(.+?)$", re.MULTILINE),
    "coordinates": re.compile(r"^Координаты:\s*(.+?)$", re.MULTILINE),
    "provider": re.compile(r"^Провайдер:\s*(.+?)$", re.MULTILINE),
    "organization": re.compile(r"^Организация:\s*(.+?)$", re.MULTILINE),
    "asn": re.compile(r"^AS:\s*(.+?)$", re.MULTILINE),
    "reverse": re.compile(r"^Обратное имя:\s*(.+?)$", re.MULTILINE),
    "connection": re.compile(r"^Тип подключения:\s*(.+?)$", re.MULTILINE),
}


@dataclass(slots=True)
class DomainIpInfo:
    ip: str | None
    country: str | None
    city: str | None
    region: str | None
    postal_code: str | None
    timezone: str | None
    local_time: str | None
    coordinates: str | None
    provider: str | None
    organization: str | None
    asn: str | None
    reverse: str | None
    connection: str | None
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "country": self.country,
            "city": self.city,
            "region": self.region,
            "postal_code": self.postal_code,
            "timezone": self.timezone,
            "local_time": self.local_time,
            "coordinates": self.coordinates,
            "provider": self.provider,
            "organization": self.organization,
            "asn": self.asn,
            "reverse": self.reverse,
            "connection": self.connection,
        }


def parse_domain_ip(text: str) -> DomainIpInfo:
    text = text or ""
    captured: dict[str, str | None] = {}
    for key, rx in _FIELDS.items():
        m = rx.search(text)
        captured[key] = m.group(1).strip() if m else None
    return DomainIpInfo(raw_text=text, **captured)


__all__ = ["DomainIpInfo", "parse_domain_ip"]
