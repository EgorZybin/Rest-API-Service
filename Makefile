VENV ?= .venv
PYTHON := $(VENV)/bin/python
RUFF := $(VENV)/bin/ruff
SHERLOCK := $(VENV)/bin/sherlock-api

.PHONY: help install run migrate-up migrate-down migrate-current dispatcher lint fmt check

help:
	@echo "Targets:"
	@echo "  install          - install project with dev deps"
	@echo "  run              - run API with reload"
	@echo "  migrate-up       - alembic upgrade head"
	@echo "  migrate-down     - alembic downgrade -1"
	@echo "  migrate-current  - show current alembic revision"
	@echo "  dispatcher       - run dispatcher"
	@echo "  lint             - ruff check"
	@echo "  fmt              - ruff format"
	@echo "  check            - compile-check package"

install:
	$(PYTHON) -m pip install -e ".[dev]"

run:
	$(SHERLOCK) serve --reload

migrate-up:
	$(SHERLOCK) db upgrade

migrate-down:
	$(SHERLOCK) db downgrade

migrate-current:
	$(SHERLOCK) db current

dispatcher:
	$(SHERLOCK) dispatcher run

lint:
	$(RUFF) check src

fmt:
	$(RUFF) format src

check:
	$(PYTHON) -m compileall -q src/sherlock_api
