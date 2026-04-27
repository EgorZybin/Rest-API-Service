"""remove api_keys feature and task ownership by key

Revision ID: 0005_remove_api_keys
Revises: 0004_webhook_retry
Create Date: 2026-04-27 09:50:00
"""
from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0005_remove_api_keys"
down_revision: str | None = "0004_webhook_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_tasks_api_key_id", table_name="tasks")
    op.drop_constraint("fk_tasks_api_key_id_api_keys", "tasks", type_="foreignkey")
    op.drop_column("tasks", "api_key_id")

    op.drop_index("ix_api_keys_role", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")


def downgrade() -> None:
    raise NotImplementedError("Downgrade is not supported for 0005_remove_api_keys")
