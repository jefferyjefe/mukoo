"""Flask application factory and HTTP routes."""

from __future__ import annotations

from typing import Optional

from flask import Flask, jsonify, request
from pydantic import ValidationError
from sqlalchemy import text

from .config import Config
from .db import make_engine
from .schemas import BatchIn
from .service import ingest_batch


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
        payload = request.get_json(silent=True)
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
