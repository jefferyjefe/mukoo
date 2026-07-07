"""Request validation for the ingestion endpoint (pydantic v2)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

NetworkType = Literal["LTE", "5G-NR", "none"]


class SampleIn(BaseModel):
    """One measurement from the field logger.

    Signal metrics (rsrp/rsrq/sinr) and cell_id are optional. A sample with
    ``network_type="none"`` and null metrics is valid dead-zone data, not an
    error — that absence of coverage is precisely what we want to record.
    """

    # `forbid` surfaces client typos (e.g. "rspr") as 400s instead of silently
    # dropping a field to null and corrupting the dataset.
    model_config = ConfigDict(extra="forbid")

    sample_id: UUID
    recorded_at: datetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    network_type: NetworkType

    rsrp: Optional[float] = None
    rsrq: Optional[float] = None
    sinr: Optional[float] = None
    cell_id: Optional[str] = None
    speed_mps: Optional[float] = Field(default=None, ge=0)
    heading_deg: Optional[float] = Field(default=None, ge=0, lt=360)

    # Optional per-sample overrides; default to the batch-level values.
    session_id: Optional[UUID] = None
    carrier: Optional[str] = None


class BatchIn(BaseModel):
    """A batch upload — normally one drive session's worth of samples."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    carrier: str = Field(min_length=1)
    samples: List[SampleIn] = Field(min_length=1)
