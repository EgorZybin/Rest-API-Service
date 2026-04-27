"""webhook retry bookkeeping columns on tasks

Revision ID: 0004_webhook_retry
Revises: 0003_tg_interactions
Create Date: 2026-04-24 01:00:00

Adds three columns used by the webhook sweeper:

- ``webhook_attempts``         — delivery attempts made so far
- ``webhook_next_attempt_at``  — next time the sweeper may retry; indexed
  so the sweeper can quickly pick the due rows
- ``webhook_error``            — last error message (truncated, for ops)

The sweeper picks rows matching:
    webhook_url IS NOT NULL
    AND webhook_delivered_at IS NULL
    AND status IN (terminal…)
    AND (webhook_next_attempt_at IS NULL OR webhook_next_attempt_at <= now())
    AND webhook_attempts < MAX_ATTEMPTS
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_webhook_retry"
down_revision: Union[str, None] = "0003_tg_interactions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "webhook_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "webhook_next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "webhook_error",
            sa.String(length=512),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_tasks_webhook_next_attempt_at",
        "tasks",
        ["webhook_next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tasks_webhook_next_attempt_at", table_name="tasks"
    )
    op.drop_column("tasks", "webhook_error")
    op.drop_column("tasks", "webhook_next_attempt_at")
    op.drop_column("tasks", "webhook_attempts")
