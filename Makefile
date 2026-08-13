UV ?= uv

.PHONY: install lint format format-check typecheck security test build check lock

install:
	$(UV) sync --locked

lint:
	$(UV) run --locked ruff check kairos_backtest tests

format:
	$(UV) run --locked ruff format kairos_backtest tests

format-check:
	$(UV) run --locked ruff format --check kairos_backtest tests

typecheck:
	$(UV) run --locked mypy kairos_backtest

security:
	$(UV) run --locked bandit -q -r kairos_backtest

test:
	$(UV) run --locked pytest -q --tb=short

build:
	$(UV) build --no-sources

check: lint format-check typecheck security test build

lock:
	$(UV) lock
