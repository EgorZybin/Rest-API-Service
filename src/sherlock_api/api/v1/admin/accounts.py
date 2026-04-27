from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sherlock_api.accounts.loader import load_accounts
from sherlock_api.api.v1.schemas.accounts import (
    AccountHealthOut,
    AccountLoadResultOut,
    AccountOut,
    AccountUpdate,
    PoolSummary,
)
from sherlock_api.auth import require_api_key
from sherlock_api.config import get_settings
from sherlock_api.db.enums import AccountStatus
from sherlock_api.db.models import Account
from sherlock_api.db.session import get_session
from sherlock_api.tg import health_check

router = APIRouter(prefix="/accounts")


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


@router.post("/health/all", response_model=list[AccountHealthOut], summary="Проверить все аккаунты")
async def health_all(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
    include_dead: Annotated[bool, Query()] = False,
) -> list[AccountHealthOut]:
    stmt = select(Account).order_by(Account.id)
    if not include_dead:
        stmt = stmt.where(Account.status != AccountStatus.dead)
    rows = (await session.execute(stmt)).scalars().all()
    out: list[AccountHealthOut] = []
    for acc in rows:
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
    return out
