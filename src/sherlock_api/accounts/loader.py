from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sherlock_api.db.enums import AccountStatus
from sherlock_api.db.models import Account
from sherlock_api.logging import get_logger

log = get_logger(__name__)


def _parse_proxy(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return {"raw": raw}

    proxy_type_map = {1: "http", 2: "socks4", 3: "socks5"}
    type_code = raw[0] if isinstance(raw[0], int) else None
    result: dict[str, Any] = {
        "raw": list(raw),
        "type_code": type_code,
        "type": proxy_type_map.get(type_code, "unknown"),
        "host": raw[1] if len(raw) > 1 else None,
        "port": raw[2] if len(raw) > 2 else None,
    }
    if len(raw) > 3 and isinstance(raw[3], bool):
        result["rdns"] = raw[3]
        if len(raw) > 4:
            result["username"] = raw[4]
        if len(raw) > 5:
            result["password"] = raw[5]
    else:
        if len(raw) > 3:
            result["username"] = raw[3]
        if len(raw) > 4:
            result["password"] = raw[4]
    return result


def _clean_phone(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lstrip("+")
    return s or None


@dataclass(slots=True)
class ParsedAccount:
    phone: str
    session_path: Path
    source_json: dict[str, Any]

    tg_user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    api_id: int | None = None
    api_hash: str | None = None
    device_model: str | None = None
    system_version: str | None = None
    app_version: str | None = None
    lang_code: str | None = None
    system_lang_code: str | None = None
    lang_pack: str | None = None
    two_fa_password: str | None = None
    proxy: dict[str, Any] | None = None

    @classmethod
    def from_files(cls, session_path: Path, json_path: Path) -> "ParsedAccount":
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        phone = _clean_phone(raw.get("phone")) or _clean_phone(session_path.stem)
        if not phone:
            raise ValueError(f"cannot determine phone for {session_path}")

        return cls(
            phone=phone,
            session_path=session_path.resolve(),
            source_json=raw,
            tg_user_id=raw.get("user_id"),
            username=(raw.get("username") or None),
            first_name=raw.get("first_name") or None,
            last_name=raw.get("last_name") or None,
            api_id=raw.get("app_id"),
            api_hash=raw.get("app_hash"),
            device_model=raw.get("device") or raw.get("device_model"),
            system_version=raw.get("sdk") or raw.get("system_version"),
            app_version=raw.get("app_version"),
            lang_code=raw.get("lang_code"),
            system_lang_code=raw.get("system_lang_code"),
            lang_pack=raw.get("lang_pack"),
            two_fa_password=raw.get("twoFA") or raw.get("two_fa") or None,
            proxy=_parse_proxy(raw.get("proxy")),
        )


@dataclass(slots=True)
class DiscoveryIssue:
    path: Path
    reason: str


@dataclass(slots=True)
class Discovery:
    accounts: list[ParsedAccount] = field(default_factory=list)
    issues: list[DiscoveryIssue] = field(default_factory=list)


def discover_accounts(root: Path) -> Discovery:
    out = Discovery()
    if not root.exists():
        out.issues.append(DiscoveryIssue(root, "accounts_dir does not exist"))
        return out

    for session_path in sorted(root.glob("*.session")):
        json_path = session_path.with_suffix(".json")
        if not json_path.exists():
            out.issues.append(DiscoveryIssue(session_path, "missing .json sidecar"))
            continue
        try:
            parsed = ParsedAccount.from_files(session_path, json_path)
        except Exception as e:
            out.issues.append(DiscoveryIssue(session_path, f"parse error: {e!r}"))
            continue
        out.accounts.append(parsed)

    return out


@dataclass(slots=True)
class AccountLoadResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    issues: list[DiscoveryIssue] = field(default_factory=list)

    @property
    def total_synced(self) -> int:
        return len(self.created) + len(self.updated)


async def load_accounts(
    session: AsyncSession,
    root: Path,
    *,
    reactivate_dead: bool = False,
) -> AccountLoadResult:
    discovery = discover_accounts(root)
    result = AccountLoadResult(issues=discovery.issues)

    if not discovery.accounts:
        return result

    phones = [p.phone for p in discovery.accounts]
    existing_rows = (
        (await session.execute(select(Account).where(Account.phone.in_(phones)))).scalars().all()
    )
    existing: dict[str, Account] = {acc.phone: acc for acc in existing_rows}

    now = datetime.now(timezone.utc)

    for parsed in discovery.accounts:
        acc = existing.get(parsed.phone)
        if acc is None:
            acc = Account(
                phone=parsed.phone,
                session_path=str(parsed.session_path),
                source_json=parsed.source_json,
                tg_user_id=parsed.tg_user_id,
                username=parsed.username,
                first_name=parsed.first_name,
                last_name=parsed.last_name,
                api_id=parsed.api_id,
                api_hash=parsed.api_hash,
                device_model=parsed.device_model,
                system_version=parsed.system_version,
                app_version=parsed.app_version,
                lang_code=parsed.lang_code,
                system_lang_code=parsed.system_lang_code,
                lang_pack=parsed.lang_pack,
                two_fa_password=parsed.two_fa_password,
                proxy=parsed.proxy,
                status=AccountStatus.new,
            )
            session.add(acc)
            result.created.append(parsed.phone)
            log.info("accounts.loader.create", phone=parsed.phone)
            continue

        acc.session_path = str(parsed.session_path)
        acc.source_json = parsed.source_json
        if parsed.tg_user_id is not None:
            acc.tg_user_id = parsed.tg_user_id
        acc.username = parsed.username
        acc.first_name = parsed.first_name
        acc.last_name = parsed.last_name
        acc.api_id = parsed.api_id
        acc.api_hash = parsed.api_hash
        acc.device_model = parsed.device_model
        acc.system_version = parsed.system_version
        acc.app_version = parsed.app_version
        acc.lang_code = parsed.lang_code
        acc.system_lang_code = parsed.system_lang_code
        acc.lang_pack = parsed.lang_pack
        acc.two_fa_password = parsed.two_fa_password
        acc.proxy = parsed.proxy

        if reactivate_dead and acc.status == AccountStatus.dead:
            acc.status = AccountStatus.new
            acc.status_reason = f"reactivated via loader at {now.isoformat()}"

        result.updated.append(parsed.phone)
        log.info("accounts.loader.update", phone=parsed.phone, status=acc.status.value)

    await session.commit()
    return result
