# Ingestion API image. Build context is the repo root so we can copy both the
# ingest package and the db/ migrations:
#   docker build -f ingest/Dockerfile -t mukoo-ingest .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libpq for psycopg2-binary at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Install the ingestion package.
COPY ingest/pyproject.toml ingest/README.md ./ingest/
COPY ingest/src ./ingest/src
RUN pip install ./ingest gunicorn

# Migrations live alongside the app so the same image can run `alembic upgrade`.
COPY db ./db
RUN pip install -r db/requirements.txt

EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "mukoo_ingest.wsgi:app"]
