.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

# Everything runs in the project's Python 3.13 virtualenv. Windows users can run
# the same targets with .\make.ps1 instead.
ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python.exe
BOOTSTRAP ?= py -3.13
else
PY := .venv/bin/python
BOOTSTRAP ?= python3.13
endif

.PHONY: help install db migrate psql test lint fixture backfill registry spike down clean logs

help:
	@echo "FireWatch - build tasks (Windows: .\\make.ps1 <target>)"
	@echo "  make install   create .venv (Python 3.13) and install dependencies"
	@echo "  make db        start postgis(+timescaledb) and wait for healthy"
	@echo "  make migrate   apply sql/*.sql in order"
	@echo "  make psql      open a psql shell in the db container"
	@echo "  make test      run pytest"
	@echo "  make lint      ruff check"
	@echo "  make fixture   stage 1: write the synthetic test fixture to data/mock"
	@echo "  make backfill  stage 2: load everything into the database, then check it"
	@echo "  make registry  stage 3: build the source registry, then check it"
	@echo "  make spike     stage 1.5: cluster one real year of FIRMS (no database)"
	@echo "  make down      stop containers (data kept)"
	@echo "  make clean     stop containers AND delete the volume"

install:
	@echo ">> creating .venv with Python 3.13 if missing"
	@test -x $(PY) || $(BOOTSTRAP) -m venv .venv
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
	@echo ">> applying sql/*.sql"
	$(PY) scripts/migrate.py

psql:
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-firewatch} -d $${POSTGRES_DB:-firewatch}

test:
	$(PY) -m pytest tests/ -v

lint:
	$(PY) -m ruff check firewatch/ tests/ scripts/

fixture:
	@echo ">> stage 1: synthetic test fixture -> data/mock"
	$(PY) scripts/generate_fixture.py

backfill:
	@echo ">> stage 2: backfill (MOCK_MODE decides real vs fixture)"
	$(PY) scripts/backfill.py
	$(PY) scripts/check_ingest.py

registry:
	@echo ">> stage 3: registry (gate, cluster, fingerprints, baselines)"
	$(PY) scripts/build_registry.py
	$(PY) scripts/check_registry.py

spike:
	@echo ">> stage 1.5: one real year of FIRMS, clustered three ways"
	$(PY) scripts/spike_cluster.py

logs:
	$(COMPOSE) logs -f db

down:
	$(COMPOSE) down

clean:
	$(COMPOSE) down -v
	@echo ">> volume removed"
