.PHONY: sync run lint test smoke check docker-up docker-logs docker-down

sync:
	uv sync

run:
	uv run remindly

lint:
	uv run ruff check . --no-cache

test:
	uv run python -B -m unittest discover -s tests

smoke:
	uv run python -B scripts/smoke_flow.py

check: lint test smoke

docker-up:
	docker compose up -d --build

docker-logs:
	docker compose logs -f remindly

docker-down:
	docker compose down
