"""Ingestion endpoint behaviour."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text


def _sample(sample_id=None, **overrides):
    sample = {
        "sample_id": str(sample_id or uuid.uuid4()),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "lat": 32.4488,
        "lon": -81.7832,
        "network_type": "LTE",
        "rsrp": -95.0,
        "rsrq": -10.5,
        "sinr": 4.0,
        "cell_id": "310-410-0001",
        "speed_mps": 12.5,
        "heading_deg": 270.0,
    }
    sample.update(overrides)
    return sample


def _batch(samples, session_id=None, carrier="Verizon"):
    return {
        "session_id": str(session_id or uuid.uuid4()),
        "carrier": carrier,
        "samples": samples,
    }


def _row_count(client) -> int:
    engine = client.application.config["ENGINE"]
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM measurements")).scalar_one()


def test_partial_duplicate_batch(client, clean_db):
    """Some samples new, some already seen: insert the new, skip the dupes."""
    ids = [uuid.uuid4() for _ in range(5)]
    session_id = uuid.uuid4()

    # First upload: samples 0, 1, 2.
    first = _batch([_sample(sample_id=ids[i]) for i in (0, 1, 2)], session_id=session_id)
    r1 = client.post("/v1/measurements", json=first)
    assert r1.status_code == 200
    assert r1.get_json() == {"received": 3, "inserted": 3, "skipped": 0}

    # Second upload overlaps: 1 and 2 are already stored; 3 and 4 are new.
    second = _batch(
        [_sample(sample_id=ids[i]) for i in (1, 2, 3, 4)],
        session_id=session_id,
    )
    r2 = client.post("/v1/measurements", json=second)
    assert r2.status_code == 200
    assert r2.get_json() == {"received": 4, "inserted": 2, "skipped": 2}

    # Exactly the five distinct samples landed.
    assert _row_count(client) == 5


def test_reposting_identical_batch_is_idempotent(client, clean_db):
    batch = _batch([_sample() for _ in range(4)])

    r1 = client.post("/v1/measurements", json=batch)
    assert r1.get_json() == {"received": 4, "inserted": 4, "skipped": 0}

    r2 = client.post("/v1/measurements", json=batch)
    assert r2.get_json() == {"received": 4, "inserted": 0, "skipped": 4}

    assert _row_count(client) == 4


def test_intra_batch_duplicate_is_collapsed(client, clean_db):
    dup_id = uuid.uuid4()
    batch = _batch([_sample(sample_id=dup_id), _sample(sample_id=dup_id)])

    r = client.post("/v1/measurements", json=batch)
    assert r.get_json() == {"received": 2, "inserted": 1, "skipped": 1}
    assert _row_count(client) == 1


def test_dead_zone_sample_is_accepted(client, clean_db):
    """Null metrics + network_type='none' is valid data, not an error."""
    dead = _sample(
        network_type="none",
        rsrp=None,
        rsrq=None,
        sinr=None,
        cell_id=None,
    )
    r = client.post("/v1/measurements", json=_batch([dead]))
    assert r.status_code == 200
    assert r.get_json() == {"received": 1, "inserted": 1, "skipped": 0}
    assert _row_count(client) == 1


def test_invalid_network_type_is_rejected(client, clean_db):
    r = client.post("/v1/measurements", json=_batch([_sample(network_type="3G")]))
    assert r.status_code == 400
    assert r.get_json()["error"] == "validation_error"
    assert _row_count(client) == 0


def test_out_of_range_coordinate_is_rejected(client, clean_db):
    r = client.post("/v1/measurements", json=_batch([_sample(lat=200.0)]))
    assert r.status_code == 400
    assert _row_count(client) == 0


def test_empty_sample_list_is_rejected(client, clean_db):
    r = client.post("/v1/measurements", json=_batch([]))
    assert r.status_code == 400
