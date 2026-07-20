# Convenience targets for local development.
# DATABASE_URL is honoured if already set in the environment.

DATABASE_URL ?= postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo
export DATABASE_URL
# The ingest suite truncates tables; it must always target the *_test database.
TEST_DATABASE_URL ?= postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo_test
export TEST_DATABASE_URL

COMPOSE = docker compose -f infra/docker-compose.yml

.PHONY: help install db-up db-down migrate test test-model krige api

help:
	@echo "install    install ingest[dev] + model[dev] + migration deps into the active venv"
	@echo "db-up      start PostGIS (detached)"
	@echo "db-down    stop the compose stack and remove volumes"
	@echo "migrate    alembic upgrade head against DATABASE_URL"
	@echo "test       run the ingest + model test suites"
	@echo "test-model run the model test suite only"
	@echo "krige      run ordinary kriging of RSRP -> surfaces in ~/mukoo"
	@echo "suggest    suggest next drive targets (needs the kriging surface) -> ~/mukoo"
	@echo "api        run the Flask API locally"

install:
	pip install -e 'ingest[dev]' -e 'model[dev]'
	pip install -r db/requirements.txt

db-up:
	$(COMPOSE) up -d db

db-down:
	$(COMPOSE) down -v

migrate:
	cd db && alembic upgrade head

test:
	pytest ingest model -v

test-model:
	pytest model -v

krige:
	mukoo-krige --metric rsrp

suggest:
	mukoo-suggest --metric rsrp

api:
	flask --app mukoo_ingest.wsgi:app run
