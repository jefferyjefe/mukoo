"""create measurements table

Revision ID: 0001
Revises:
Create Date: 2026-07-07

First migration for Mukoo. Enables PostGIS and creates the measurements table
that the ingestion API writes to, including the unique idempotency key on
sample_id and a GiST spatial index on geom.
"""
from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostGIS provides the geometry type and spatial indexing used below.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "measurements",
        # Surrogate primary key; keeps the (larger) sample_id UUID free to serve
        # purely as the idempotency key.
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        # Client-generated idempotency key.
        sa.Column("sample_id", UUID(as_uuid=True), nullable=False),
        # Groups every sample from a single drive.
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        # The phone's timestamp for the reading.
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        # POINT in WGS84 (SRID 4326). spatial_index=False: we create the GiST
        # index explicitly below so its name and options are under our control.
        sa.Column(
            "geom",
            geoalchemy2.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        # Signal metrics — nullable, because a dead zone has nothing to report.
        sa.Column("rsrp", sa.Numeric(), nullable=True),
        sa.Column("rsrq", sa.Numeric(), nullable=True),
        sa.Column("sinr", sa.Numeric(), nullable=True),
        # "none" == no serving cell (dead zone).
        sa.Column("network_type", sa.Text(), nullable=False),
        sa.Column("cell_id", sa.Text(), nullable=True),
        sa.Column("speed_mps", sa.Numeric(), nullable=True),
        sa.Column("heading_deg", sa.Numeric(), nullable=True),
        sa.Column("carrier", sa.Text(), nullable=False),
        # Server-side receipt time.
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("sample_id", name="uq_measurements_sample_id"),
        sa.CheckConstraint(
            "network_type IN ('LTE', '5G-NR', 'none')",
            name="ck_measurements_network_type",
        ),
    )

    # Spatial index for geographic queries (bounding box, nearest, coverage tiles).
    op.create_index(
        "ix_measurements_geom",
        "measurements",
        ["geom"],
        postgresql_using="gist",
    )
    # Sessions are queried as a unit when reconstructing a drive.
    op.create_index("ix_measurements_session_id", "measurements", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_measurements_session_id", table_name="measurements")
    op.drop_index("ix_measurements_geom", table_name="measurements")
    op.drop_table("measurements")
    # The postgis extension is intentionally left in place; other objects may
    # depend on it and dropping it is a heavier, separate operational decision.
