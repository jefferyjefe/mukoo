"""Flask application factory and HTTP routes."""

from __future__ import annotations

import gzip
import hashlib
import json
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request
from pydantic import ValidationError
from sqlalchemy import text

from .config import Config
from .db import make_engine
from .schemas import BatchIn
from .service import ingest_batch


def _request_payload():
    """The request body as parsed JSON, or None if it isn't valid.

    The field logger gzips its batches (Content-Encoding: gzip) because a
    200-sample JSON body compresses ~10x and smaller bodies fail less over a
    flaky rural link. Werkzeug does not transparently decompress request
    bodies, so we do it here; plain JSON continues to work unchanged.
    """
    if request.content_encoding == "gzip":
        try:
            raw = gzip.decompress(request.get_data(cache=False))
            return json.loads(raw)
        except (OSError, ValueError):
            return None
    return request.get_json(silent=True)


def create_app(config: Optional[Config] = None) -> Flask:
    config = config or Config.from_env()

    app = Flask(__name__)
    app.config["MUKOO"] = config
    engine = make_engine(config.database_url)
    app.config["ENGINE"] = engine

    @app.get("/healthz")
    def healthz():
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # pragma: no cover - exercised via ops, not unit tests
            return jsonify(status="db_unavailable"), 503
        return jsonify(status="ok"), 200

    @app.post("/v1/measurements")
    def post_measurements():
        payload = _request_payload()
        if payload is None:
            return jsonify(error="invalid_json", detail="Request body must be a JSON object"), 400

        # Reject oversized batches before validating every element.
        samples = payload.get("samples") if isinstance(payload, dict) else None
        if isinstance(samples, list) and len(samples) > config.max_batch:
            return (
                jsonify(
                    error="batch_too_large",
                    detail=f"A batch may contain at most {config.max_batch} samples",
                ),
                400,
            )

        try:
            batch = BatchIn.model_validate(payload)
        except ValidationError as exc:
            return jsonify(error="validation_error", detail=exc.errors(include_url=False)), 400

        result = ingest_batch(engine, batch)
        return jsonify(result.as_dict()), 200

    @app.get("/v1/stats")
    def get_stats():
        """Summary of everything ingested: the dataset at a glance.

        Two round-trips of aggregate SQL — totals, dead zones, RSRP spread,
        bounding box, and a per-session breakdown (chronological). Read-only
        and cheap (~3k rows today; the per-session GROUP BY rides the
        session_id index), so the field logger or a curl can poll it freely.

        Also counts *latched re-reads*: consecutive samples in a session that
        re-report one modem reading rather than observing something new. Those
        rows are not independent, so a high share means the effective sample
        size is well below the row count — worth seeing here without having to
        run the model.
        """
        totals_sql = text(
            """
            SELECT count(*) AS total,
                   count(DISTINCT session_id) AS sessions,
                   count(*) FILTER (WHERE network_type = 'none') AS dead_zones,
                   min(rsrp) AS rsrp_min,
                   max(rsrp) AS rsrp_max,
                   round(avg(rsrp), 2) AS rsrp_avg,
                   min(ST_Y(geom)) AS lat_min, max(ST_Y(geom)) AS lat_max,
                   min(ST_X(geom)) AS lon_min, max(ST_X(geom)) AS lon_max,
                   max(received_at) AS last_received_at
            FROM measurements
            """
        )
        # A row is a re-read of its predecessor when the modem stamped both with
        # the same modem_reported_at, or when the whole signal triple is
        # unchanged. Deliberately the same rule as the model's load-time dedupe
        # (mukoo_model.data._RUN_START_SUBQUERY) — the two packages stay
        # decoupled by design, so if that rule changes, change it here too.
        #
        # modem_timestamps.rows vs .distinct is the diagnostic for whether the
        # modem's clock can identify re-reads at all: if they are equal, the
        # framework restamped every poll and only the value test is meaningful.
        sessions_sql = text(
            """
            WITH seq AS (
                SELECT session_id, network_type, rsrp, rsrq, sinr, recorded_at,
                       modem_reported_at,
                       lag(session_id)        OVER w AS p_session,
                       lag(modem_reported_at) OVER w AS p_modem,
                       lag(rsrp)              OVER w AS p_rsrp,
                       lag(rsrq)              OVER w AS p_rsrq,
                       lag(sinr)              OVER w AS p_sinr
                FROM measurements
                WINDOW w AS (PARTITION BY session_id ORDER BY recorded_at, id)
            )
            SELECT session_id::text AS session_id,
                   count(*) AS samples,
                   min(recorded_at) AS started_at,
                   max(recorded_at) AS ended_at,
                   count(*) FILTER (WHERE network_type = 'none') AS dead_zones,
                   min(rsrp) AS rsrp_min,
                   max(rsrp) AS rsrp_max,
                   round(avg(rsrp), 2) AS rsrp_avg,
                   count(*) FILTER (
                       WHERE p_session IS NOT NULL
                         AND ((modem_reported_at IS NOT NULL
                               AND p_modem IS NOT NULL
                               AND modem_reported_at = p_modem)
                              OR (rsrp IS NOT DISTINCT FROM p_rsrp
                                  AND rsrq IS NOT DISTINCT FROM p_rsrq
                                  AND sinr IS NOT DISTINCT FROM p_sinr))
                   ) AS rereads,
                   count(modem_reported_at) AS modem_rows,
                   count(DISTINCT modem_reported_at) AS modem_distinct
            FROM seq
            GROUP BY session_id
            ORDER BY min(recorded_at)
            """
        )
        try:
            with engine.connect() as conn:
                t = conn.execute(totals_sql).one()
                sessions = conn.execute(sessions_sql).all()
        except Exception:  # pragma: no cover - exercised via ops
            return jsonify(error="db_unavailable"), 503

        def _num(v):
            return float(v) if v is not None else None

        # Summed from the per-session figures: a re-read is only meaningful
        # within one drive, so there is nothing to count across the whole table.
        rereads = sum(s.rereads for s in sessions)
        modem_rows = sum(s.modem_rows for s in sessions)

        return jsonify(
            total=t.total,
            sessions=t.sessions,
            dead_zones=t.dead_zones,
            rereads={
                "rows": rereads,
                "share": round(rereads / t.total, 4) if t.total else None,
                # What the model actually gets to work with once runs collapse.
                "independent_rows": t.total - rereads,
                "with_modem_timestamp": modem_rows,
            },
            rsrp={
                "min": _num(t.rsrp_min),
                "max": _num(t.rsrp_max),
                "avg": _num(t.rsrp_avg),
            },
            bbox=(
                {
                    "lat_min": _num(t.lat_min),
                    "lat_max": _num(t.lat_max),
                    "lon_min": _num(t.lon_min),
                    "lon_max": _num(t.lon_max),
                }
                if t.total
                else None
            ),
            last_received_at=(
                t.last_received_at.isoformat() if t.last_received_at else None
            ),
            per_session=[
                {
                    "session_id": s.session_id,
                    "samples": s.samples,
                    "started_at": s.started_at.isoformat(),
                    "ended_at": s.ended_at.isoformat(),
                    "dead_zones": s.dead_zones,
                    "rsrp": {
                        "min": _num(s.rsrp_min),
                        "max": _num(s.rsrp_max),
                        "avg": _num(s.rsrp_avg),
                    },
                    "rereads": s.rereads,
                    # rows == distinct means the modem restamped every poll, so
                    # its clock cannot identify re-reads on this device.
                    "modem_timestamps": {
                        "rows": s.modem_rows,
                        "distinct": s.modem_distinct,
                    },
                }
                for s in sessions
            ],
        )

    @app.get("/v1/suggestions")
    def get_suggestions():
        """Serve the active-learning drive suggestions as GeoJSON.

        A lightweight read of the file the model package writes
        (``mukoo-suggest`` -> ``rsrp_drive_suggestions.geojson``). The field
        logger fetches this when online and caches it locally, so this endpoint
        stays read-only and does not touch the database. 404 until suggestions
        have been generated; the client then falls back to its own last cache.
        """
        path = Path(config.suggestions_path)
        if not path.is_file():
            return (
                jsonify(
                    error="no_suggestions",
                    detail="No drive suggestions have been generated yet",
                ),
                404,
            )
        try:
            body = path.read_text()
            json.loads(body)  # guard against serving a truncated/partial write
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            return (
                jsonify(
                    error="unreadable_suggestions",
                    detail="Suggestions file is missing or not valid JSON",
                ),
                500,
            )

        # Conditional GET: the field logger re-checks on every map open, over a
        # rural link — an unchanged file should cost a 304, not a re-download.
        # Strong ETag over the bytes (content identity, robust to mtime games);
        # Last-Modified as the fallback validator for simpler clients.
        etag = f'"{hashlib.md5(body.encode("utf-8")).hexdigest()}"'
        last_modified = format_datetime(
            datetime.fromtimestamp(mtime, tz=timezone.utc), usegmt=True
        )
        headers = {"ETag": etag, "Last-Modified": last_modified}

        if_none_match = request.headers.get("If-None-Match")
        if if_none_match and etag in [v.strip() for v in if_none_match.split(",")]:
            return Response(status=304, headers=headers)
        if if_none_match is None:  # If-None-Match wins over If-Modified-Since
            ims = request.headers.get("If-Modified-Since")
            if ims == last_modified:
                return Response(status=304, headers=headers)

        # Raw pass-through with the GeoJSON media type; the phone parses it.
        return Response(body, mimetype="application/geo+json", headers=headers)

    return app
