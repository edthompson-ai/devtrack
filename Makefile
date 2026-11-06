.PHONY: install test test-pg lint typecheck check demo serve bench seed-snapshot clean

install:
	pip install -e ".[dev,postgres]"

test:
	pytest

# Same suite on Postgres. Point TEST_DATABASE_URL at a throwaway database.
test-pg:
	TEST_DATABASE_URL=$${TEST_DATABASE_URL:-postgresql://devtrack:devtrack-local-only@localhost:5432/devtrack_test} pytest

lint:
	ruff check src tests scripts
	ruff format --check src tests scripts

typecheck:
	mypy

check: lint typecheck test

demo:
	devtrack demo

serve:
	devtrack serve

# Before/after index benchmark. Needs an EMPTY database (it adds ~400k rows).
bench:
	DATABASE_URL=$${BENCH_DATABASE_URL:-sqlite:///bench.db} devtrack bench

# Rebuild the offline seed from local clones of the three repos.
seed-snapshot:
	python scripts/build_seed_from_git.py ../click pallets/click
	python scripts/build_seed_from_git.py ../requests psf/requests
	python scripts/build_seed_from_git.py ../httpx encode/httpx

clean:
	rm -f devtrack.db devtrack.db-* bench.db bench.db-*
