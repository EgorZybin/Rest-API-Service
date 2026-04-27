"""accounts: add bootstrapped_at + last_bot_ack_at

Revision ID: 0002_account_bootstrap
Revises: 0001_initial
Create Date: 2026-04-24 00:00:00

Adds two per-account timestamps used by the dispatcher:

- ``bootstrapped_at``: when the account successfully completed ``/start`` with
  the target bot (i.e. the bot actually replied with its intro). Lets workers
  skip the bootstrap step on subsequent tasks.
- ``last_bot_ack_at``: wall-clock of the most recent bot message received by
  this account. Used to detect "account looks authorized but the bot went
  silent" (whitelist revoked, bot banned it, etc.).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_account_bootstrap"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "bootstrapped_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "last_bot_ack_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("accounts", "last_bot_ack_at")
    op.drop_column("accounts", "bootstrapped_at")
