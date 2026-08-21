.PHONY: dev test run docker daily scan update

dev:
	uvicorn app.main:app --reload

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

test:
	PYTHONPATH=. pytest -q

docker:
	docker compose up --build

update:
	python scripts/update_data.py

scan:
	python scripts/scan_today.py

daily:
	python scripts/run_daily.py
