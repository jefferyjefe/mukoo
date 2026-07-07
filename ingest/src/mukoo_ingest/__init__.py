"""Mukoo ingestion API.

A small Flask service that accepts batches of cellular-signal measurements from
the field logger and writes them to PostGIS. Ingestion is idempotent: clients
generate a ``sample_id`` per measurement and may safely re-send batches.
"""

from .app import create_app

__all__ = ["create_app"]
__version__ = "0.1.0"
