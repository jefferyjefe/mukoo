"""Ingestion endpoint behaviour."""

from __future__ import annotations

import gzip
import json
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


def test_gzip_batch_is_accepted(client, clean_db):
    """The field logger gzips its batches; the server must decompress them."""
    batch = _batch([_sample() for _ in range(3)])
    body = gzip.compress(json.dumps(batch).encode("utf-8"))

    r = client.post(
        "/v1/measurements",
        data=body,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert r.status_code == 200
    assert r.get_json() == {"received": 3, "inserted": 3, "skipped": 0}
    assert _row_count(client) == 3


def test_corrupt_gzip_body_is_rejected(client, clean_db):
    """Garbage bytes under a gzip header must 400, never 500."""
    r = client.post(
        "/v1/measurements",
        data=b"\x1f\x8b not actually gzip",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_json"
    assert _row_count(client) == 0


# -- modem_reported_at (schema 0002) ---------------------------------------
#
# The logger samples faster than the modem refreshes its reading, so several
# consecutive samples can carry one latched measurement. modem_reported_at makes
# those identifiable here rather than guessable from equal values. It is optional
# on purpose: older logger builds must keep uploading unchanged.


def _modem_times(client):
    engine = client.application.config["ENGINE"]
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT modem_reported_at FROM measurements "
                "ORDER BY recorded_at, sample_id"
            )
        ).scalars().all()


def test_modem_reported_at_is_stored(client, clean_db):
    stamp = datetime(2026, 7, 25, 12, 30, 15, tzinfo=timezone.utc)
    body = _batch([_sample(modem_reported_at=stamp.isoformat())])
    r = client.post("/v1/measurements", json=body)
    assert r.status_code == 200
    assert r.get_json() == {"received": 1, "inserted": 1, "skipped": 0}
    assert _modem_times(client) == [stamp]


def test_old_format_batch_without_modem_reported_at_still_uploads(client, clean_db):
    """The existing ingestion contract: a payload that predates 0002 is valid."""
    body = _batch([_sample() for _ in range(3)])
    assert "modem_reported_at" not in body["samples"][0]
    r = client.post("/v1/measurements", json=body)
    assert r.status_code == 200
    assert r.get_json() == {"received": 3, "inserted": 3, "skipped": 0}
    # Absent, not zero or epoch: these rows genuinely have no modem timestamp.
    assert _modem_times(client) == [None, None, None]


def test_explicit_null_modem_reported_at_accepted(client, clean_db):
    """A client may send the key as null (dead zone: no cell, so no timestamp)."""
    body = _batch([_sample(modem_reported_at=None, network_type="none",
                           rsrp=None, rsrq=None, sinr=None, cell_id=None)])
    r = client.post("/v1/measurements", json=body)
    assert r.status_code == 200
    assert _modem_times(client) == [None]


def test_mixed_batch_old_and_new_samples(client, clean_db):
    """A v2 logger draining a buffer that still holds pre-v2 rows."""
    stamp = datetime(2026, 7, 25, 9, 0, 0, tzinfo=timezone.utc)
    body = _batch(
        [
            _sample(recorded_at=datetime(2026, 7, 25, 8, 59, 0,
                                         tzinfo=timezone.utc).isoformat()),
            _sample(
                recorded_at=datetime(2026, 7, 25, 9, 0, 0,
                                     tzinfo=timezone.utc).isoformat(),
                modem_reported_at=stamp.isoformat(),
            ),
        ]
    )
    r = client.post("/v1/measurements", json=body)
    assert r.status_code == 200
    assert r.get_json() == {"received": 2, "inserted": 2, "skipped": 0}
    assert _modem_times(client) == [None, stamp]


def test_repeated_modem_reported_at_marks_latched_rereads(client, clean_db):
    """Three samples, one underlying measurement — the point of the column.

    The rows differ in position and sample_id but share the modem's timestamp,
    so a consumer can collapse them without comparing signal values.
    """
    stamp = datetime(2026, 7, 25, 14, 0, 0, tzinfo=timezone.utc)
    session_id = uuid.uuid4()
    samples = [
        _sample(
            recorded_at=datetime(2026, 7, 25, 14, 0, i, tzinfo=timezone.utc).isoformat(),
            modem_reported_at=stamp.isoformat(),
            lat=32.4488 + i * 0.001,
        )
        for i in range(3)
    ]
    r = client.post("/v1/measurements", json=_batch(samples, session_id=session_id))
    assert r.status_code == 200

    engine = client.application.config["ENGINE"]
    with engine.connect() as conn:
        distinct = conn.execute(
            text(
                "SELECT count(DISTINCT modem_reported_at), count(*) "
                "FROM measurements WHERE session_id = :sid"
            ),
            {"sid": str(session_id)},
        ).one()
    assert distinct == (1, 3)


def test_malformed_modem_reported_at_is_rejected(client, clean_db):
    body = _batch([_sample(modem_reported_at="not-a-timestamp")])
    r = client.post("/v1/measurements", json=body)
    assert r.status_code == 400
    assert _row_count(client) == 0


def test_unknown_field_still_rejected(client, clean_db):
    """Adding an optional field must not loosen the forbid-extra guard."""
    body = _batch([_sample(modem_reportd_at="2026-07-25T12:00:00Z")])  # typo
    r = client.post("/v1/measurements", json=body)
    assert r.status_code == 400
    assert _row_count(client) == 0
