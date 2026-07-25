"""add modem_reported_at to measurements

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-25

The field logger samples on a fixed cadence (~3.5 s), but the modem refreshes its
signal report far more slowly — so several consecutive samples can carry the same
latched reading re-read, not independent observations. Until now that could only
be *inferred* after the fact by comparing values, which cannot tell a genuine
repeat measurement apart from a re-read of a stale one.

``modem_reported_at`` records the modem's own timestamp for the reading
(Android ``CellInfo.getTimestampMillis()``, converted from the boot-relative
clock to wall time on the device). Consecutive rows sharing one
``modem_reported_at`` are re-reads of a single measurement, identifiable at
ingestion time.

Nullable on purpose, and it stays nullable: every row written before this column
existed has no such timestamp, and an older build of the logger — or a modem
that does not supply one — still uploads valid samples without it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No server_default and no backfill: NULL is the honest value for rows whose
    # client never reported one, and adding a nullable column with no default is
    # a metadata-only change in Postgres — no table rewrite, no lock held on the
    # existing rows.
    op.add_column(
        "measurements",
        sa.Column("modem_reported_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Finding the re-read runs within a drive means walking a session in time
    # order and grouping by this column, which is exactly this index's shape.
    op.create_index(
        "ix_measurements_session_modem_reported_at",
        "measurements",
        ["session_id", "modem_reported_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_measurements_session_modem_reported_at", table_name="measurements"
    )
    op.drop_column("measurements", "modem_reported_at")
