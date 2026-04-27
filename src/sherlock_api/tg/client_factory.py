from __future__ import annotations

import asyncio
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import socks
from python_socks import ProxyType
from python_socks.async_.asyncio import Proxy
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    PhoneNumberBannedError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from sherlock_api.config import get_settings
from sherlock_api.db.enums import AccountStatus
from sherlock_api.db.models import Account
from sherlock_api.logging import get_logger

log = get_logger(__name__)

PROBE_TIMEOUT = 15.0
CONNECT_TIMEOUT = 25.0
PROFILE_RESPONSE_TIMEOUT = 45.0
_DEFAULT_DC = ("149.154.167.51", 443)

_PYSOCKS_TO_PYTHON_SOCKS = {
    socks.SOCKS5: ProxyType.SOCKS5,
    socks.SOCKS4: ProxyType.SOCKS4,
    socks.HTTP: ProxyType.HTTP,
}


class AccountConnectionError(RuntimeError):
    pass


@dataclass(slots=True)
class AccountHealthReport:
    ok: bool
    status: AccountStatus
    reason: str
    me: dict[str, Any] | None = None
    bot_id: int | None = None
    bot_username: str | None = None
    elapsed_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status.value,
            "reason": self.reason,
            "me": self.me,
            "bot_id": self.bot_id,
            "bot_username": self.bot_username,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(slots=True)
class AccountProfileReport:
    ok: bool
    reason: str
    user_id: str | None = None
    available_searches: int | None = None
    balance: float | None = None
    referral_balance: float | None = None
    registered_at: str | None = None
    raw_text: str | None = None
    elapsed_ms: int | None = None


_PROXY_TYPE_MAP = {
    "http": socks.HTTP,
    "socks4": socks.SOCKS4,
    "socks5": socks.SOCKS5,
}

_RE_PROFILE_ID = re.compile(r"Ваш ID:\s*([^\n\r]+)")
_RE_PROFILE_SEARCHES = re.compile(r"Доступно поисков:\s*(\d+)")
_RE_PROFILE_BALANCE = re.compile(r"Ваш баланс:\s*\$?\s*([0-9]+(?:[.,][0-9]+)?)")
_RE_PROFILE_REF_BALANCE = re.compile(
    r"Реферальный баланс:\s*\$?\s*([0-9]+(?:[.,][0-9]+)?)"
)
_RE_PROFILE_REGISTERED = re.compile(r"Дата регистрации:\s*([^\n\r]+)")


def _parse_money(raw: str | None) -> float | None:
    if not raw:
        return None
    val = raw.strip().replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return None


def _parse_profile_text(text: str) -> dict[str, Any]:
    user_id_m = _RE_PROFILE_ID.search(text)
    searches_m = _RE_PROFILE_SEARCHES.search(text)
    balance_m = _RE_PROFILE_BALANCE.search(text)
    ref_balance_m = _RE_PROFILE_REF_BALANCE.search(text)
    registered_m = _RE_PROFILE_REGISTERED.search(text)
    return {
        "user_id": user_id_m.group(1).strip() if user_id_m else None,
        "available_searches": int(searches_m.group(1)) if searches_m else None,
        "balance": _parse_money(balance_m.group(1) if balance_m else None),
        "referral_balance": _parse_money(ref_balance_m.group(1) if ref_balance_m else None),
        "registered_at": registered_m.group(1).strip() if registered_m else None,
    }


def resolve_proxy(raw: dict[str, Any] | None) -> tuple | None:
    if not raw:
        return None

    proxy_type_name = (raw.get("type") or "").lower()
    sock_type = _PROXY_TYPE_MAP.get(proxy_type_name)
    if sock_type is None:
        log.warning("tg.proxy.unsupported", type=proxy_type_name, raw=raw)
        return None

    host = raw.get("host")
    port = raw.get("port")
    if not host or not port:
        return None

    rdns = bool(raw.get("rdns", True))
    user = raw.get("username")
    pwd = raw.get("password")
    return (sock_type, host, int(port), rdns, user, pwd)


def _session_dc(session_path: str | None) -> tuple[str, int] | None:
    if not session_path:
        return None
    path = session_path
    if not path.endswith(".session"):
        path = f"{path}.session"
    if not Path(path).exists():
        return None
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute("SELECT server_address, port FROM sessions LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.warning("tg.session.read_failed", path=path, err=repr(e))
        return None
    if not row or not row[0] or not row[1]:
        return None
    return (row[0], int(row[1]))


async def probe_network(account: Account, *, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    dc_host, dc_port = _session_dc(account.session_path) or _DEFAULT_DC
    proxy_tuple = resolve_proxy(account.proxy)

    try:
        if proxy_tuple is None:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(dc_host, dc_port), timeout=timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True, f"direct tcp ok ({dc_host}:{dc_port})"

        sock_type, host, port, rdns, user, pwd = proxy_tuple
        ptype = _PYSOCKS_TO_PYTHON_SOCKS.get(sock_type)
        if ptype is None:
            return False, f"unsupported proxy type: {sock_type}"
        proxy = Proxy.create(ptype, host, port, user, pwd, rdns=rdns)
        sock = await asyncio.wait_for(
            proxy.connect(dest_host=dc_host, dest_port=dc_port),
            timeout=timeout,
        )
        try:
            sock.close()
        except Exception:
            pass
        return True, f"proxy tcp ok ({dc_host}:{dc_port})"
    except asyncio.TimeoutError:
        return False, f"probe timeout after {timeout:.0f}s"
    except Exception as e:
        return False, f"probe failed: {type(e).__name__}: {e}"


def build_client(
    account: Account,
    *,
    connection_retries: int = 2,
    request_retries: int = 2,
    session_path_override: str | None = None,
    timeout_override: float | None = None,
) -> TelegramClient:
    settings = get_settings()

    if not account.api_id or not account.api_hash:
        raise AccountConnectionError(f"account {account.phone} has no api_id/api_hash")
    if not account.session_path and not session_path_override:
        raise AccountConnectionError(f"account {account.phone} has no session_path")

    device_kwargs: dict[str, Any] = {}
    if account.device_model:
        device_kwargs["device_model"] = account.device_model
    if account.system_version:
        device_kwargs["system_version"] = account.system_version
    if account.app_version:
        device_kwargs["app_version"] = account.app_version
    if account.lang_code:
        device_kwargs["lang_code"] = account.lang_code
    if account.system_lang_code:
        device_kwargs["system_lang_code"] = account.system_lang_code

    proxy = resolve_proxy(account.proxy)

    session_path = session_path_override or account.session_path
    timeout = timeout_override or settings.account_response_timeout

    return TelegramClient(
        session_path,
        account.api_id,
        account.api_hash,
        proxy=proxy,
        timeout=timeout,
        request_retries=request_retries,
        connection_retries=connection_retries,
        auto_reconnect=False,
        **device_kwargs,
    )


def _session_sqlite_path(session_path: str | None) -> Path | None:
    if not session_path:
        return None
    path = Path(session_path)
    if path.suffix != ".session":
        path = path.with_suffix(".session")
    if not path.exists():
        return None
    return path


async def health_check(
    account: Account,
    *,
    bot_username: str | None = None,
) -> AccountHealthReport:
    settings = get_settings()
    target_bot = bot_username or settings.sherlock_bot_username

    loop = asyncio.get_running_loop()
    started = loop.time()

    try:
        client = build_client(account, connection_retries=0, request_retries=0)
    except AccountConnectionError as e:
        account.status = AccountStatus.dead
        account.status_reason = str(e)
        return AccountHealthReport(ok=False, status=account.status, reason=str(e))

    probe_ok, probe_reason = await probe_network(account)
    if not probe_ok:
        account.status = AccountStatus.dead
        account.status_reason = f"proxy/network: {probe_reason}"
        log.info(
            "tg.health.proxy_dead",
            phone=account.phone,
            proxy=account.proxy,
            reason=probe_reason,
        )
        return AccountHealthReport(
            ok=False,
            status=account.status,
            reason=account.status_reason,
        )

    try:
        try:
            await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            reason = (
                "telegram silent on handshake (auth_key likely revoked; "
                f"probe passed: {probe_reason})"
            )
            account.status = AccountStatus.unauthorized
            account.status_reason = reason
            return AccountHealthReport(ok=False, status=account.status, reason=reason)
        except (
            AuthKeyUnregisteredError,
            AuthKeyDuplicatedError,
            SessionRevokedError,
        ) as e:
            reason = f"auth key invalid: {type(e).__name__}"
            account.status = AccountStatus.unauthorized
            account.status_reason = reason
            return AccountHealthReport(ok=False, status=account.status, reason=reason)
        except (ConnectionError, OSError) as e:
            reason = f"connect failed: {type(e).__name__}: {e}"
            account.status = AccountStatus.dead
            account.status_reason = reason
            return AccountHealthReport(ok=False, status=account.status, reason=reason)

        try:
            if not await client.is_user_authorized():
                reason = "session not authorized"
                account.status = AccountStatus.unauthorized
                account.status_reason = reason
                return AccountHealthReport(ok=False, status=account.status, reason=reason)

            try:
                me = await client.get_me()
            except (
                AuthKeyUnregisteredError,
                AuthKeyDuplicatedError,
                SessionRevokedError,
            ) as e:
                reason = f"auth key invalid: {type(e).__name__}"
                account.status = AccountStatus.unauthorized
                account.status_reason = reason
                return AccountHealthReport(ok=False, status=account.status, reason=reason)
            except (
                UserDeactivatedBanError,
                UserDeactivatedError,
                PhoneNumberBannedError,
            ) as e:
                reason = f"banned: {type(e).__name__}"
                account.status = AccountStatus.banned
                account.status_reason = reason
                return AccountHealthReport(ok=False, status=account.status, reason=reason)
            except SessionPasswordNeededError:
                reason = "2FA password needed"
                account.status = AccountStatus.unauthorized
                account.status_reason = reason
                return AccountHealthReport(ok=False, status=account.status, reason=reason)

            if me is None:
                reason = "get_me returned None"
                account.status = AccountStatus.unauthorized
                account.status_reason = reason
                return AccountHealthReport(ok=False, status=account.status, reason=reason)

            me_dict: dict[str, Any] = {
                "id": me.id,
                "username": me.username,
                "phone": me.phone,
                "first_name": me.first_name,
                "last_name": me.last_name,
                "is_premium": bool(getattr(me, "premium", False)),
                "is_bot": bool(getattr(me, "bot", False)),
            }

            account.tg_user_id = me.id
            account.username = me.username
            account.first_name = me.first_name
            account.last_name = me.last_name

            bot_id = None
            bot_uname = None
            try:
                bot = await client.get_entity(target_bot)
                bot_id = getattr(bot, "id", None)
                bot_uname = getattr(bot, "username", None)
            except Exception as e:
                log.warning(
                    "tg.health.bot_resolve_failed",
                    phone=account.phone,
                    bot=target_bot,
                    err=repr(e),
                )

            flip_to_idle = account.status in {
                AccountStatus.new,
                AccountStatus.dead,
                AccountStatus.unauthorized,
                AccountStatus.banned,
                AccountStatus.limited,
                AccountStatus.subscription_expired,
            }
            if flip_to_idle:
                account.status = AccountStatus.idle
                account.status_reason = "healthy"

            elapsed_ms = int((loop.time() - started) * 1000)
            return AccountHealthReport(
                ok=True,
                status=account.status,
                reason="healthy",
                me=me_dict,
                bot_id=bot_id,
                bot_username=bot_uname,
                elapsed_ms=elapsed_ms,
            )
        finally:
            await client.disconnect()
    except Exception as e:
        reason = f"unexpected: {type(e).__name__}: {e}"
        account.status = AccountStatus.dead
        account.status_reason = reason
        log.exception("tg.health.unexpected", phone=account.phone)
        return AccountHealthReport(ok=False, status=account.status, reason=reason)


async def profile_check(
    account: Account,
    *,
    bot_username: str | None = None,
) -> AccountProfileReport:
    settings = get_settings()
    target_bot = bot_username or settings.sherlock_bot_username

    loop = asyncio.get_running_loop()
    started = loop.time()

    original_session = _session_sqlite_path(account.session_path)
    if original_session is None:
        return AccountProfileReport(ok=False, reason="session file does not exist")

    try:
        with tempfile.TemporaryDirectory(prefix=f"profile-{account.id}-") as tmp_dir:
            session_copy = Path(tmp_dir) / original_session.name
            try:
                shutil.copy2(original_session, session_copy)
            except Exception as e:
                return AccountProfileReport(
                    ok=False,
                    reason=f"failed to copy session file: {type(e).__name__}: {e}",
                )

            try:
                client = build_client(
                    account,
                    connection_retries=0,
                    request_retries=0,
                    session_path_override=str(session_copy),
                    timeout_override=PROFILE_RESPONSE_TIMEOUT,
                )
            except AccountConnectionError as e:
                return AccountProfileReport(ok=False, reason=str(e))

            probe_ok, probe_reason = await probe_network(account)
            if not probe_ok:
                return AccountProfileReport(ok=False, reason=f"proxy/network: {probe_reason}")

            try:
                await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
                if not await client.is_user_authorized():
                    return AccountProfileReport(ok=False, reason="session not authorized")

                async with client.conversation(target_bot, timeout=PROFILE_RESPONSE_TIMEOUT) as conv:
                    await conv.send_message("Показать меню")
                    menu_msg = await conv.get_response()

                    profile_btn_text: str | None = None
                    if getattr(menu_msg, "buttons", None):
                        for row in menu_msg.buttons:
                            for btn in row:
                                text = str(getattr(btn, "text", "") or "")
                                if "профил" in text.lower():
                                    profile_btn_text = text
                                    break
                            if profile_btn_text:
                                break
                    if not profile_btn_text:
                        return AccountProfileReport(
                            ok=False,
                            reason="profile button not found in menu",
                        )

                    await menu_msg.click(text=profile_btn_text)

                    profile_msg = await conv.get_edit()
                    raw_text = str(profile_msg.message or "")
                    parsed = _parse_profile_text(raw_text)

                    elapsed_ms = int((loop.time() - started) * 1000)
                    ok = parsed["user_id"] is not None or parsed["available_searches"] is not None
                    return AccountProfileReport(
                        ok=ok,
                        reason="ok" if ok else "profile info not found in bot response",
                        user_id=parsed["user_id"],
                        available_searches=parsed["available_searches"],
                        balance=parsed["balance"],
                        referral_balance=parsed["referral_balance"],
                        registered_at=parsed["registered_at"],
                        raw_text=raw_text or None,
                        elapsed_ms=elapsed_ms,
                    )
            except (TimeoutError, asyncio.TimeoutError):
                elapsed_ms = int((loop.time() - started) * 1000)
                return AccountProfileReport(
                    ok=False,
                    reason=f"bot response timeout after {PROFILE_RESPONSE_TIMEOUT:.0f}s",
                    elapsed_ms=elapsed_ms,
                )
            except Exception as e:
                return AccountProfileReport(ok=False, reason=f"unexpected: {type(e).__name__}: {e}")
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
    except Exception as e:
        return AccountProfileReport(ok=False, reason=f"unexpected: {type(e).__name__}: {e}")
