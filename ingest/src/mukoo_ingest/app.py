"""Flask application factory and HTTP routes."""

from __future__ import annotations

import gzip
import json
from typing import Optional

from flask import Flask, jsonify, request
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

    return app
