"""WSGI entrypoint for gunicorn: ``gunicorn mukoo_ingest.wsgi:app``."""

from __future__ import annotations

from .app import create_app

app = create_app()
