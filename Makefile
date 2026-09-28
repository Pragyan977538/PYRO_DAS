.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose
PY := python3

.PHONY: help install db migrate psql test lint down clean logs

help:
	@echo "FireWatch — stage 0"
	@echo "  make install   install python dependencies"
	@echo "  make db        start postgis+timescaledb and wait for healthy"
	@echo "  make migrate   apply sql/001_schema.sql then sql/002_indexes.sql"
	@echo "  make psql      open a psql shell in the db container"
	@echo "  make test      run pytest"
	@echo "  make lint      ruff check"
	@echo "  make down      stop containers (data kept)"
	@echo "  make clean     stop containers AND delete the volume"

install:
	@echo ">> installing python dependencies"
	$(PY) -m pip install -r requirements.txt
	@test -f .env || (cp .env.example .env && echo ">> created .env from .env.example")

db:
	@echo ">> starting database"
	$(COMPOSE) up -d db
	@echo ">> waiting for healthy (timescale/postgis image is a large first pull)"
	@for i in $$(seq 1 60); do \
		status=$$($(COMPOSE) ps --format json db 2>/dev/null | grep -o "\"Health\":\"[a-z]*\"" | head -1 | cut -d: -f2 | tr -d "\"") ; \
		if [ "$$status" = "healthy" ]; then echo ">> database healthy"; exit 0; fi ; \
		sleep 3 ; \
	done ; \
	echo ">> timed out waiting for the database"; $(COMPOSE) logs --tail=30 db; exit 1

migrate:
	@echo ">> applying schema"
	$(PY) -c "from firewatch.db import run_sql_file; run_sql_file(\"sql/001_schema.sql\")"
	@echo ">> applying indexes"
	$(PY) -c "from firewatch.db import run_sql_file; run_sql_file(\"sql/002_indexes.sql\")"
	@echo ">> migration complete"

psql:
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-firewatch} -d $${POSTGRES_DB:-firewatch}

test:
	$(PY) -m pytest tests/ -v

lint:
	$(PY) -m ruff check firewatch/ tests/

logs:
	$(COMPOSE) logs -f db

down:
	$(COMPOSE) down

clean:
	$(COMPOSE) down -v
	@echo ">> volume removed"
