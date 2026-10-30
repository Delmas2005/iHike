# iHike developer commands. Everything runs offline except `install` and `requirements`.
PYTHON ?= python3.11
VENV   ?= .venv
BIN    := $(VENV)/bin

.PHONY: help install install-ai run test lint format bench requirements clean

help:  ## List the commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-13s %s\n", $$1, $$2}'

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

install: $(BIN)/python  ## Create .venv with the app and the dev tools, install the git hooks
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev]" -c requirements.txt  # same versions as the Space
	$(BIN)/pre-commit install

install-ai: install  ## Also install the in-Space models' libraries (CPU PyTorch, about 1 GB)
	$(BIN)/python -m pip install -e ".[ai]" -c requirements.txt

run:  ## Serve the app on http://localhost:7860 (reads .env if present)
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; $(BIN)/python app.py

test:  ## Run the tests with coverage (no network, no keys, no models)
	$(BIN)/python -m pytest --cov --cov-report=term

lint:  ## Lint, check formatting and types
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

format:  ## Apply formatting and safe lint fixes
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

bench:  ## Benchmarks (ParseBench and performance; added in phase 5)
	@echo "No benchmark yet: ParseBench and the performance benchmarks arrive in phase 5." && exit 1

requirements:  ## Regenerate the pinned requirements.txt used by the Space (needs uv)
	scripts/update_requirements.sh

clean:  ## Remove caches and local data
	rm -rf .mypy_cache .ruff_cache .coverage htmlcov data pending_photos
	find . -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} +
