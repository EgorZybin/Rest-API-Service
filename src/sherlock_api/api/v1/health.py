from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sherlock_api import __version__
from sherlock_api.db.enums import AccountStatus
from sherlock_api.db.models import Account
from sherlock_api.db.session import get_session

router = APIRouter()


class PoolSummary(BaseModel):
    total: int
    by_status: dict[str, int]


class HealthResponse(BaseModel):
    status: str
    version: str
    db: str
    pool: PoolSummary | None = None


@router.get("/health", response_model=HealthResponse)
async def health(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HealthResponse:
    db_status = "ok"
    pool: PoolSummary | None = None
    try:
        await session.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {type(e).__name__}"
        return HealthResponse(status="degraded", version=__version__, db=db_status)

    try:
        total = (await session.execute(select(func.count()).select_from(Account))).scalar_one()
        rows = (
            await session.execute(select(Account.status, func.count()).group_by(Account.status))
        ).all()
        by_status = {s.value: 0 for s in AccountStatus}
        for st, cnt in rows:
            by_status[st.value if hasattr(st, "value") else str(st)] = cnt
        pool = PoolSummary(total=total, by_status=by_status)
    except Exception:
        pool = None

    overall = "ok"
    if pool is not None:
        usable = pool.by_status.get("idle", 0) + pool.by_status.get("busy", 0)
        if pool.total > 0 and usable == 0:
            overall = "degraded"

    return HealthResponse(status=overall, version=__version__, db=db_status, pool=pool)
