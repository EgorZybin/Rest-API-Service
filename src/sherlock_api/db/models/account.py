from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, DateTime, Enum, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sherlock_api.db.base import Base, TimestampMixin
from sherlock_api.db.enums import AccountStatus

if TYPE_CHECKING:
    from sherlock_api.db.models.task import Task


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)

    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    session_path: Mapped[str] = mapped_column(String(512), nullable=False)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    api_id: Mapped[int | None] = mapped_column(nullable=True)
    api_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    system_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lang_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    system_lang_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lang_pack: Mapped[str | None] = mapped_column(String(32), nullable=True)
    two_fa_password: Mapped[str | None] = mapped_column(String(256), nullable=True)

    proxy: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, name="account_status", native_enum=False, length=32),
        default=AccountStatus.new,
        nullable=False,
        index=True,
    )
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscription_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    requests_total: Mapped[int] = mapped_column(default=0, nullable=False)
    requests_last_hour: Mapped[int] = mapped_column(default=0, nullable=False)
    hour_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolve_requests_today: Mapped[int] = mapped_column(default=0, nullable=False)
    resolve_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    bootstrapped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_bot_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tasks: Mapped[list[Task]] = relationship(
        back_populates="account",
        cascade="save-update",
        passive_deletes=True,
    )

    __table_args__ = (Index("ix_accounts_status_last_request", "status", "last_request_at"),)

    def __repr__(self) -> str:
        return f"<Account id={self.id} phone={self.phone} status={self.status.value}>"
