.PHONY: help test lint type check format build smoke clean

SMOKE_VENV ?= /tmp/smoke
SMOKE_OUTPUT ?= $(HOME)/work/telemetry-smoke/package-results

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

test: ## Run tests
	uv run pytest

lint: ## Check lint and formatting
	uv run ruff check .
	uv run ruff check src/ --select D,D401 --ignore D105,D107,D417
	uv run ruff format --check .

type: ## Run strict type checkers
	uv run mypy src/ tests/
	uv run pyright

check: lint type test ## Run all quality gates

format: ## Format and fix lint
	uv run ruff format .
	uv run ruff check --fix .

build: ## Build and validate wheel and sdist
	uv build
	uvx twine check dist/*

smoke: ## Build and smoke an installed wheel outside the checkout (includes OTLP)
	uv venv --allow-existing "$(SMOKE_VENV)"
	uv run python smoke/install_wheel.py "$(SMOKE_VENV)"
	uv pip install --python "$(SMOKE_VENV)" 'opentelemetry-exporter-otlp-proto-http>=1.39,<2'
	"$(SMOKE_VENV)/bin/python" smoke/run_package.py "$(SMOKE_OUTPUT)"

clean: ## Remove build and scratch output
	rm -rf dist/ .tmp/
