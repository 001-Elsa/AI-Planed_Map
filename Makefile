.PHONY: dev migrate test lint format eval compose

dev: migrate
	python -m uvicorn backend.app.main:app --reload --port 3000

migrate:
	alembic upgrade head

test:
	python -m pytest -c backend/pytest.ini

lint:
	ruff check backend
	ruff format --check backend
	mypy backend/app

format:
	ruff check --fix backend
	ruff format backend

eval:
	python backend/tests/evaluation/evaluate_intent.py
	python backend/tests/evaluation/evaluate_agents.py
	python backend/tests/evaluation/evaluate_routes.py
	python backend/tests/evaluation/evaluate_multi_agent.py
	python backend/tests/evaluation/evaluate_model_router.py
	python backend/tests/evaluation/replay_agent_benchmark.py

compose:
	docker compose up --build
