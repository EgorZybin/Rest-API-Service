from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sherlock_api.db.enums import AccountStatus


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: str
    tg_user_id: int | None
    username: str | None
    first_name: str | None
    last_name: str | None
    status: AccountStatus
    status_reason: str | None
    api_id: int | None
    device_model: str | None
    system_version: str | None
    app_version: str | None
    lang_code: str | None
    proxy: dict[str, Any] | None
    subscription_status: str | None
    subscription_expires_at: datetime | None
    requests_total: int
    requests_last_hour: int
    resolve_requests_today: int = 0
    last_request_at: datetime | None
    last_error_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class AccountUpdate(BaseModel):
    status: AccountStatus | None = Field(
        default=None,
        description="Принудительно задать статус аккаунта.",
    )
    status_reason: str | None = None
    proxy: dict[str, Any] | None = None
    two_fa_password: str | None = None
    subscription_status: str | None = None
    subscription_expires_at: datetime | None = None


class AccountLoadResultOut(BaseModel):
    created: list[str]
    updated: list[str]
    issues: list[dict[str, str]]
    total_synced: int


class AccountHealthOut(BaseModel):
    account_id: int
    phone: str
    ok: bool
    status: AccountStatus
    reason: str
    me: dict[str, Any] | None = None
    bot_id: int | None = None
    bot_username: str | None = None
    elapsed_ms: int | None = None


class AccountHealthAllOut(BaseModel):
    total: int
    checked: int
    busy_skipped_count: int
    rows: list[AccountHealthOut]


class AccountProfileOut(BaseModel):
    account_id: int
    phone: str
    ok: bool
    reason: str
    user_id: str | None = None
    available_searches: int | None = None
    balance: float | None = None
    referral_balance: float | None = None
    registered_at: str | None = None
    raw_text: str | None = None
    elapsed_ms: int | None = None


class AccountProfilesStatsOut(BaseModel):
    total: int
    ok: int
    failed: int
    total_available_searches: int
    total_balance: float
    total_referral_balance: float


class AccountProfileRowOut(BaseModel):
    account_id: int
    phone: str
    ok: bool
    reason: str
    user_id: str | None = None
    available_searches: int | None = None
    balance: float | None = None
    referral_balance: float | None = None
    registered_at: str | None = None
    elapsed_ms: int | None = None


class AccountProfilesDashboardOut(BaseModel):
    stats: AccountProfilesStatsOut
    rows: list[AccountProfileRowOut]


class PoolSummary(BaseModel):
    total: int
    by_status: dict[str, int]
