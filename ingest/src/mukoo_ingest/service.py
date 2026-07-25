"""Ingestion logic: turn a validated batch into rows and upsert idempotently."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from geoalchemy2.elements import WKTElement
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from .models import measurements
from .schemas import BatchIn, SampleIn

SRID = 4326


@dataclass(frozen=True)
class IngestResult:
    received: int
    inserted: int
    skipped: int

    def as_dict(self) -> Dict[str, int]:
        return {
            "received": self.received,
            "inserted": self.inserted,
            "skipped": self.skipped,
        }


def _row(sample: SampleIn, batch: BatchIn) -> Dict[str, object]:
    return {
        "sample_id": sample.sample_id,
        "session_id": sample.session_id or batch.session_id,
        "recorded_at": sample.recorded_at,
        # None for older clients that do not report it; the column is nullable.
        "modem_reported_at": sample.modem_reported_at,
        # PostGIS POINT is (x=lon, y=lat).
        "geom": WKTElement(f"POINT({sample.lon} {sample.lat})", srid=SRID),
        "rsrp": sample.rsrp,
        "rsrq": sample.rsrq,
        "sinr": sample.sinr,
        "network_type": sample.network_type,
        "cell_id": sample.cell_id,
        "speed_mps": sample.speed_mps,
        "heading_deg": sample.heading_deg,
        "carrier": sample.carrier or batch.carrier,
    }


def ingest_batch(engine: Engine, batch: BatchIn) -> IngestResult:
    """Insert new samples, skip ones already seen, never fail on duplicates.

    Idempotency has two layers:

    1. Intra-batch: if the same ``sample_id`` appears twice in one request we
       keep the first occurrence. (ON CONFLICT DO NOTHING would also collapse
       these, but de-duping in Python keeps the returned counts unambiguous.)
    2. Cross-request: ``ON CONFLICT (sample_id) DO NOTHING`` skips samples that
       already exist in the table from an earlier upload.

    ``inserted`` is derived from the RETURNING clause, which yields only the
    rows actually written; ``skipped`` is everything else in the payload.
    """
    received = len(batch.samples)

    seen = set()
    rows: List[Dict[str, object]] = []
    for sample in batch.samples:
        if sample.sample_id in seen:
            continue
        seen.add(sample.sample_id)
        rows.append(_row(sample, batch))

    stmt = (
        pg_insert(measurements)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["sample_id"])
        .returning(measurements.c.sample_id)
    )

    with engine.begin() as conn:
        result = conn.execute(stmt)
        inserted = len(result.fetchall())

    return IngestResult(
        received=received,
        inserted=inserted,
        skipped=received - inserted,
    )
