"""GET /v1/stats: the ingested dataset at a glance."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _sample(sample_id=None, **overrides):
    sample = {
        "sample_id": str(sample_id or uuid.uuid4()),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "lat": 32.4488,
        "lon": -81.7832,
        "network_type": "LTE",
        "rsrp": -95.0,
    }
    sample.update(overrides)
    return sample


def _batch(samples, session_id=None, carrier="Verizon"):
    return {
        "session_id": str(session_id or uuid.uuid4()),
        "carrier": carrier,
        "samples": samples,
    }


def test_stats_empty_table(client, clean_db):
    r = client.get("/v1/stats")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 0
    assert body["sessions"] == 0
    assert body["dead_zones"] == 0
    assert body["rsrp"] == {"min": None, "max": None, "avg": None}
    assert body["bbox"] is None
    assert body["last_received_at"] is None
    assert body["per_session"] == []


def test_stats_reflects_ingested_data(client, clean_db):
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    # Session 1: two LTE points with known RSRP spread and locations.
    client.post(
        "/v1/measurements",
        json=_batch(
            [
                _sample(rsrp=-80.0, lat=32.40, lon=-81.80),
                _sample(rsrp=-120.0, lat=32.44, lon=-81.70),
            ],
            session_id=s1,
        ),
    )
    # Session 2: one dead-zone sample (no rsrp at all).
    client.post(
        "/v1/measurements",
        json=_batch(
            [_sample(network_type="none", rsrp=None, lat=32.42, lon=-81.75)],
            session_id=s2,
        ),
    )

    r = client.get("/v1/stats")
    assert r.status_code == 200
    body = r.get_json()

    assert body["total"] == 3
    assert body["sessions"] == 2
    assert body["dead_zones"] == 1
    assert body["rsrp"] == {"min": -120.0, "max": -80.0, "avg": -100.0}
    assert body["bbox"] == {
        "lat_min": 32.40,
        "lat_max": 32.44,
        "lon_min": -81.80,
        "lon_max": -81.70,
    }
    assert body["last_received_at"] is not None

    per_session = {s["session_id"]: s for s in body["per_session"]}
    assert set(per_session) == {str(s1), str(s2)}
    assert per_session[str(s1)]["samples"] == 2
    assert per_session[str(s1)]["dead_zones"] == 0
    assert per_session[str(s1)]["rsrp"]["min"] == -120.0
    assert per_session[str(s2)]["samples"] == 1
    assert per_session[str(s2)]["dead_zones"] == 1
    assert per_session[str(s2)]["rsrp"] == {"min": None, "max": None, "avg": None}
    # Chronological ordering: session 1 was ingested first.
    assert body["per_session"][0]["session_id"] == str(s1)
