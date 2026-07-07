# Convenience targets for local development.
# DATABASE_URL is honoured if already set in the environment.

DATABASE_URL ?= postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo
export DATABASE_URL

COMPOSE = docker compose -f infra/docker-compose.yml

.PHONY: help install db-up db-down migrate test api

help:
	@echo "install   install ingest[dev] + model + migration deps into the active venv"
	@echo "db-up     start PostGIS (detached)"
	@echo "db-down   stop the compose stack and remove volumes"
	@echo "migrate   alembic upgrade head against DATABASE_URL"
	@echo "test      run the ingest test suite"
	@echo "api       run the Flask API locally"

install:
	pip install -e 'ingest[dev]' -e model
	pip install -r db/requirements.txt

db-up:
	$(COMPOSE) up -d db

db-down:
	$(COMPOSE) down -v

migrate:
	cd db && alembic upgrade head

test:
	pytest ingest -v

api:
	flask --app mukoo_ingest.wsgi:app run
