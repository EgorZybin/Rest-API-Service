"""append-only tg_interactions audit table

Revision ID: 0003_tg_interactions
Revises: 0002_account_bootstrap
Create Date: 2026-04-24 00:05:00

Used to trace every outgoing and incoming TG event tied to a task, so we can
forensically reconstruct what the bot saw / replied when a client disputes a
result. Append-only; no updates/deletes in hot path.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_tg_interactions"
down_revision: Union[str, None] = "0002_account_bootstrap"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tg_interactions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tasks.id",
                ondelete="SET NULL",
                name="fk_tg_interactions_task_id_tasks",
            ),
            nullable=True,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey(
                "accounts.id",
                ondelete="SET NULL",
                name="fk_tg_interactions_account_id_accounts",
            ),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("tg_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_tg_interactions_task_id", "tg_interactions", ["task_id"])
    op.create_index(
        "ix_tg_interactions_account_id", "tg_interactions", ["account_id"]
    )
    op.create_index("ix_tg_interactions_kind", "tg_interactions", ["kind"])
    op.create_index("ix_tg_interactions_at", "tg_interactions", ["at"])
    op.create_index(
        "ix_tg_interactions_task_at", "tg_interactions", ["task_id", "at"]
    )


def downgrade() -> None:
    op.drop_index("ix_tg_interactions_task_at", table_name="tg_interactions")
    op.drop_index("ix_tg_interactions_at", table_name="tg_interactions")
    op.drop_index("ix_tg_interactions_kind", table_name="tg_interactions")
    op.drop_index("ix_tg_interactions_account_id", table_name="tg_interactions")
    op.drop_index("ix_tg_interactions_task_id", table_name="tg_interactions")
    op.drop_table("tg_interactions")
