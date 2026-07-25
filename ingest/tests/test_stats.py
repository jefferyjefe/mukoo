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


# -- latched re-reads ------------------------------------------------------
#
# The logger outruns the modem, so consecutive samples can re-report one
# reading. /v1/stats surfaces that share so the effective sample size is visible
# without running the model.


def _at(second, **overrides):
    return _sample(
        recorded_at=datetime(2026, 7, 25, 12, 0, second, tzinfo=timezone.utc).isoformat(),
        **overrides,
    )


def test_stats_counts_rereads_by_unchanged_values(client, clean_db):
    session_id = uuid.uuid4()
    # 4 samples, one changed reading in the middle: rows 2 and 4 repeat their
    # predecessor, so 2 of 4 are re-reads and 2 are independent.
    samples = [
        _at(0, rsrp=-95.0), _at(3, rsrp=-95.0),
        _at(6, rsrp=-99.0), _at(9, rsrp=-99.0),
    ]
    assert client.post("/v1/measurements", json=_batch(samples, session_id=session_id)).status_code == 200

    body = client.get("/v1/stats").get_json()
    assert body["rereads"]["rows"] == 2
    assert body["rereads"]["independent_rows"] == 2
    assert body["rereads"]["share"] == 0.5
    assert body["per_session"][0]["rereads"] == 2


def test_stats_counts_rereads_by_shared_modem_timestamp(client, clean_db):
    """A shared modem stamp marks a re-read even when the values differ."""
    session_id = uuid.uuid4()
    stamp = datetime(2026, 7, 25, 11, 59, 58, tzinfo=timezone.utc).isoformat()
    other = datetime(2026, 7, 25, 12, 0, 20, tzinfo=timezone.utc).isoformat()
    samples = [
        _at(0, rsrp=-95.0, modem_reported_at=stamp),
        _at(3, rsrp=-96.0, modem_reported_at=stamp),   # re-read: same stamp
        _at(6, rsrp=-99.0, modem_reported_at=other),   # new measurement
    ]
    assert client.post("/v1/measurements", json=_batch(samples, session_id=session_id)).status_code == 200

    body = client.get("/v1/stats").get_json()
    assert body["rereads"]["rows"] == 1
    assert body["rereads"]["with_modem_timestamp"] == 3
    s = body["per_session"][0]
    # rows > distinct: the modem's clock held steady across a re-read, so it can
    # be used to identify them. Equal counts would mean it restamps every poll.
    assert s["modem_timestamps"] == {"rows": 3, "distinct": 2}


def test_stats_rereads_do_not_span_sessions(client, clean_db):
    """The first sample of a drive is never a re-read of the previous drive."""
    a, b = uuid.uuid4(), uuid.uuid4()
    assert client.post("/v1/measurements", json=_batch([_at(0, rsrp=-95.0)], session_id=a)).status_code == 200
    assert client.post("/v1/measurements", json=_batch([_at(3, rsrp=-95.0)], session_id=b)).status_code == 200

    body = client.get("/v1/stats").get_json()
    assert body["total"] == 2
    assert body["rereads"]["rows"] == 0
    assert body["rereads"]["independent_rows"] == 2


def test_stats_rereads_absent_modem_timestamp_reported_as_zero(client, clean_db):
    """Old-format rows: no modem stamp, so the value test does all the work."""
    session_id = uuid.uuid4()
    samples = [_at(0, rsrp=-95.0), _at(3, rsrp=-95.0)]
    assert client.post("/v1/measurements", json=_batch(samples, session_id=session_id)).status_code == 200

    body = client.get("/v1/stats").get_json()
    assert body["rereads"]["rows"] == 1
    assert body["rereads"]["with_modem_timestamp"] == 0
    assert body["per_session"][0]["modem_timestamps"] == {"rows": 0, "distinct": 0}


def test_stats_empty_table_reports_zero_rereads(client, clean_db):
    body = client.get("/v1/stats").get_json()
    assert body["rereads"] == {
        "rows": 0,
        "share": None,
        "independent_rows": 0,
        "with_modem_timestamp": 0,
    }
