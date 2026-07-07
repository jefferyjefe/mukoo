"""SQLAlchemy Core table definitions used by the ingestion service.

The Alembic migrations in ``db/`` are the source of truth for the schema. This
module mirrors the ``measurements`` table so the app can build typed INSERT
statements; it is intentionally not used to create the table at runtime.
"""

from __future__ import annotations

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

# The valid values for measurements.network_type. "none" is a real, expected
# value: it means the phone had no serving cell (a dead zone), which is exactly
# the signal this project exists to map.
NETWORK_TYPES = ("LTE", "5G-NR", "none")

metadata = MetaData()

measurements = Table(
    "measurements",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    # Client-generated idempotency key; unique constraint is the ON CONFLICT target.
    Column("sample_id", UUID(as_uuid=True), nullable=False),
    # Groups all samples from a single drive.
    Column("session_id", UUID(as_uuid=True), nullable=False),
    # The phone's timestamp for the measurement.
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    # spatial_index=False: the migration owns the GiST index explicitly, so we
    # keep GeoAlchemy2 from auto-emitting one here.
    Column(
        "geom",
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    ),
    # Signal metrics are nullable: in a dead zone there is nothing to measure.
    Column("rsrp", Numeric, nullable=True),
    Column("rsrq", Numeric, nullable=True),
    Column("sinr", Numeric, nullable=True),
    Column("network_type", Text, nullable=False),
    Column("cell_id", Text, nullable=True),
    Column("speed_mps", Numeric, nullable=True),
    Column("heading_deg", Numeric, nullable=True),
    Column("carrier", Text, nullable=False),
    # Server-side receipt time; DB owns the default.
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("sample_id", name="uq_measurements_sample_id"),
    CheckConstraint(
        "network_type IN ('LTE', '5G-NR', 'none')",
        name="ck_measurements_network_type",
    ),
)
