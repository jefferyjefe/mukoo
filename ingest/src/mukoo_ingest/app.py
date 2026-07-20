"""Flask application factory and HTTP routes."""

from __future__ import annotations

import gzip
import json
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
        except (OSError, ValueError):
            return (
                jsonify(
                    error="unreadable_suggestions",
                    detail="Suggestions file is missing or not valid JSON",
                ),
                500,
            )
        # Raw pass-through with the GeoJSON media type; the phone parses it.
        return Response(body, mimetype="application/geo+json")

    return app
