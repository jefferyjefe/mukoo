"""Tests for GET /v1/suggestions.

This endpoint only reads a file, never the database, so these tests build the
app directly with a Config pointing at a temp path — no DB fixture, no skip when
Postgres is absent. make_engine is lazy, so an unused/bogus database_url is fine.
"""

from __future__ import annotations

import json

import pytest

from mukoo_ingest.app import create_app
from mukoo_ingest.config import Config

SAMPLE_GEOJSON = {
    "type": "FeatureCollection",
    "properties": {"metric": "rsrp", "count": 1},
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-81.66815, 32.44140]},
            "properties": {
                "rank": 1,
                "metric": "rsrp",
                "stddev": 8.49,
                "stddev_unit": "dBm",
                "road_name": "Burkhalter Road",
                "road_distance_m": 162.0,
            },
        }
    ],
}


def _client(suggestions_path):
    app = create_app(
        Config(
            database_url="postgresql+psycopg2://unused",
            suggestions_path=str(suggestions_path),
        )
    )
    return app.test_client()


def test_returns_geojson_when_file_present(tmp_path):
    f = tmp_path / "rsrp_drive_suggestions.geojson"
    f.write_text(json.dumps(SAMPLE_GEOJSON))
    client = _client(f)

    r = client.get("/v1/suggestions")
    assert r.status_code == 200
    assert r.mimetype == "application/geo+json"
    body = r.get_json()
    assert body["type"] == "FeatureCollection"
    assert body["features"][0]["properties"]["rank"] == 1
    assert body["features"][0]["properties"]["road_name"] == "Burkhalter Road"


def test_404_when_no_suggestions_file(tmp_path):
    client = _client(tmp_path / "does_not_exist.geojson")
    r = client.get("/v1/suggestions")
    assert r.status_code == 404
    assert r.get_json()["error"] == "no_suggestions"


def test_500_when_file_is_not_valid_json(tmp_path):
    f = tmp_path / "rsrp_drive_suggestions.geojson"
    f.write_text("{ this is not valid json")  # e.g. a half-finished write
    client = _client(f)

    r = client.get("/v1/suggestions")
    assert r.status_code == 500
    assert r.get_json()["error"] == "unreadable_suggestions"


def test_body_is_served_verbatim(tmp_path):
    # The endpoint passes the file through unchanged (no re-serialisation), so a
    # client can rely on byte-for-byte what the model wrote.
    f = tmp_path / "rsrp_drive_suggestions.geojson"
    raw = json.dumps(SAMPLE_GEOJSON, indent=2)
    f.write_text(raw)
    client = _client(f)

    r = client.get("/v1/suggestions")
    assert r.get_data(as_text=True) == raw
