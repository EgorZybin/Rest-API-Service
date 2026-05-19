"""accounts: add resolve username daily quota counters

Revision ID: 0006_account_resolve_quota
Revises: 0005_remove_api_keys
Create Date: 2026-05-19 09:59:24
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_account_resolve_quota"
down_revision: Union[str, None] = "0005_remove_api_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("resolve_requests_today", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "resolve_window_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.alter_column("accounts", "resolve_requests_today", server_default=None)


def downgrade() -> None:
    op.drop_column("accounts", "resolve_window_started_at")
    op.drop_column("accounts", "resolve_requests_today")
