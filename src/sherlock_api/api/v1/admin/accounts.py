from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sherlock_api.accounts.loader import load_accounts
from sherlock_api.api.v1.schemas.accounts import (
    AccountHealthAllOut,
    AccountHealthOut,
    AccountLoadResultOut,
    AccountOut,
    AccountProfileOut,
    AccountProfileRowOut,
    AccountProfilesDashboardOut,
    AccountProfilesStatsOut,
    AccountUpdate,
    PoolSummary,
)
from sherlock_api.auth import require_api_key
from sherlock_api.config import get_settings
from sherlock_api.db.enums import AccountStatus
from sherlock_api.db.models import Account
from sherlock_api.db.session import get_session
from sherlock_api.tg import health_check, profile_check

router = APIRouter(prefix="/accounts")
PROFILE_ALL_CONCURRENCY = 5


@router.get("", response_model=list[AccountOut], summary="Список аккаунтов")
async def list_accounts(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
    q: Annotated[
        str | None,
        Query(description="Поиск по телефону или username"),
    ] = None,
    status: Annotated[AccountStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Account]:
    stmt = select(Account).order_by(Account.id).limit(limit).offset(offset)
    if status is not None:
        stmt = stmt.where(Account.status == status)
    if q:
        q_norm = q.strip().lower()
        stmt = stmt.where(
            (Account.phone.ilike(f"%{q_norm}%")) | (Account.username.ilike(f"%{q_norm}%"))
        )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


@router.get("/summary", response_model=PoolSummary, summary="Сводка по статусам пула")
async def pool_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> PoolSummary:
    rows = (await session.execute(select(Account.status))).scalars().all()
    counts = Counter(s.value for s in rows)
    return PoolSummary(total=len(rows), by_status=dict(counts))


@router.patch("/{account_id}", response_model=AccountOut, summary="Обновить аккаунт")
async def update_account(
    account_id: int,
    payload: AccountUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> Account:
    acc = await session.get(Account, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="account not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(acc, k, v)
    await session.commit()
    await session.refresh(acc)
    return acc


@router.post(
    "/{account_id}/deactivate",
    summary="Деактивировать аккаунт",
)
async def deactivate_account(
    account_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> dict[str, bool]:
    acc = await session.get(Account, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="account not found")
    acc.status = AccountStatus.dead
    acc.status_reason = "deactivated via admin API"
    await session.commit()
    return {"ok": True}


@router.post(
    "/{account_id}/activate",
    summary="Активировать аккаунт",
)
async def activate_account(
    account_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> dict[str, bool]:
    acc = await session.get(Account, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="account not found")
    acc.status = AccountStatus.idle
    acc.status_reason = "activated via admin API"
    await session.commit()
    return {"ok": True}


@router.delete(
    "/{account_id}/delete",
    summary="Полностью удалить аккаунт из БД",
)
async def purge_account(
    account_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> dict[str, bool]:
    acc = await session.get(Account, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="account not found")
    await session.delete(acc)
    await session.commit()
    return {"ok": True}


@router.post("/load", response_model=AccountLoadResultOut, summary="Пересканировать ACCOUNTS_DIR")
async def load_accounts_from_dir(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
    reactivate_dead: Annotated[bool, Query()] = False,
) -> AccountLoadResultOut:
    root = get_settings().accounts_dir
    result = await load_accounts(session, Path(root), reactivate_dead=reactivate_dead)
    issues = [{"path": str(i.path), "reason": i.reason} for i in result.issues]
    return AccountLoadResultOut(
        created=result.created,
        updated=result.updated,
        issues=issues,
        total_synced=result.total_synced,
    )


@router.post("/upload", response_model=AccountLoadResultOut, summary="Загрузить .session + .json")
async def upload_account_pair(
    session_file: Annotated[UploadFile, File(description="Файл .session")],
    json_file: Annotated[UploadFile, File(description="Файл .json")],
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
    phone: Annotated[
        str | None,
        Form(description="Переопределить основу имени файла"),
    ] = None,
) -> AccountLoadResultOut:
    settings = get_settings()
    root = Path(settings.accounts_dir)
    root.mkdir(parents=True, exist_ok=True)

    stem = (phone or Path(session_file.filename or "").stem or "").strip()
    if not stem:
        raise HTTPException(status_code=400, detail="phone or session filename is required")

    session_path = root / f"{stem}.session"
    json_path = root / f"{stem}.json"

    session_bytes = await session_file.read()
    json_bytes = await json_file.read()
    if not session_bytes or not json_bytes:
        raise HTTPException(status_code=400, detail="empty upload")

    session_path.write_bytes(session_bytes)
    json_path.write_bytes(json_bytes)

    result = await load_accounts(session, root)
    issues = [{"path": str(i.path), "reason": i.reason} for i in result.issues]
    return AccountLoadResultOut(
        created=result.created,
        updated=result.updated,
        issues=issues,
        total_synced=result.total_synced,
    )


@router.post("/{account_id}/health", response_model=AccountHealthOut, summary="Проверить аккаунт")
async def account_health(
    account_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> AccountHealthOut:
    acc = await session.get(Account, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="account not found")
    report = await health_check(acc)
    await session.commit()
    return AccountHealthOut(
        account_id=acc.id,
        phone=acc.phone,
        ok=report.ok,
        status=report.status,
        reason=report.reason,
        me=report.me,
        bot_id=report.bot_id,
        bot_username=report.bot_username,
        elapsed_ms=report.elapsed_ms,
    )


@router.post(
    "/health/all",
    response_model=AccountHealthAllOut,
    summary="Проверить все аккаунты",
)
async def health_all(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
    include_dead: Annotated[bool, Query()] = False,
    probe_busy: Annotated[
        bool,
        Query(
            description=(
                "Если false (по умолчанию), аккаунты со статусом busy не трогаются "
                "сетевым health-check, чтобы не конкурировать за session sqlite."
            )
        ),
    ] = False,
) -> AccountHealthAllOut:
    stmt = select(Account).order_by(Account.id)
    if not include_dead:
        stmt = stmt.where(Account.status != AccountStatus.dead)
    rows = (await session.execute(stmt)).scalars().all()
    out: list[AccountHealthOut] = []
    busy_skipped_count = 0
    for acc in rows:
        if acc.status == AccountStatus.busy and not probe_busy:
            busy_skipped_count += 1
            out.append(
                AccountHealthOut(
                    account_id=acc.id,
                    phone=acc.phone,
                    ok=True,
                    status=acc.status,
                    reason="skipped: account is busy (set probe_busy=true to force check)",
                )
            )
            continue
        report = await health_check(acc)
        out.append(
            AccountHealthOut(
                account_id=acc.id,
                phone=acc.phone,
                ok=report.ok,
                status=report.status,
                reason=report.reason,
                me=report.me,
                bot_id=report.bot_id,
                bot_username=report.bot_username,
                elapsed_ms=report.elapsed_ms,
            )
        )
    await session.commit()
    return AccountHealthAllOut(
        total=len(rows),
        checked=len(rows) - busy_skipped_count,
        busy_skipped_count=busy_skipped_count,
        rows=out,
    )


@router.post(
    "/profile/all",
    response_model=AccountProfilesDashboardOut,
    summary="Снять профиль по всем аккаунтам",
)
async def profile_all(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> AccountProfilesDashboardOut:
    stmt = select(Account).order_by(Account.id)
    stmt = stmt.where(Account.status != AccountStatus.dead)
    rows = (await session.execute(stmt)).scalars().all()
    sem = asyncio.Semaphore(PROFILE_ALL_CONCURRENCY)

    async def _one(acc: Account) -> AccountProfileOut:
        async with sem:
            report = await profile_check(acc)
            return AccountProfileOut(
                account_id=acc.id,
                phone=acc.phone,
                ok=report.ok,
                reason=report.reason,
                user_id=report.user_id,
                available_searches=report.available_searches,
                balance=report.balance,
                referral_balance=report.referral_balance,
                registered_at=report.registered_at,
                raw_text=report.raw_text,
                elapsed_ms=report.elapsed_ms,
            )

    out = await asyncio.gather(*(_one(acc) for acc in rows))
    rows_out = [
        AccountProfileRowOut(
            account_id=item.account_id,
            phone=item.phone,
            ok=item.ok,
            reason=item.reason,
            user_id=item.user_id,
            available_searches=item.available_searches,
            balance=item.balance,
            referral_balance=item.referral_balance,
            registered_at=item.registered_at,
            elapsed_ms=item.elapsed_ms,
        )
        for item in out
    ]
    stats = AccountProfilesStatsOut(
        total=len(out),
        ok=sum(1 for item in out if item.ok),
        failed=sum(1 for item in out if not item.ok),
        total_available_searches=sum(item.available_searches or 0 for item in out),
        total_balance=round(sum(item.balance or 0.0 for item in out), 2),
        total_referral_balance=round(sum(item.referral_balance or 0.0 for item in out), 2),
    )
    return AccountProfilesDashboardOut(stats=stats, rows=rows_out)
