.PHONY: install dev test lint docker-up docker-down migrate seed init-db

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest -q

lint:
	python -m compileall app

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

migrate:
	alembic upgrade head

seed:
	python scripts/seed_demo_data.py

init-db:
	python scripts/init_db.py
