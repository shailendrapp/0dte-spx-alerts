.PHONY: install test morning intraday eod clean

install:
	pip install -r requirements.txt

test:
	pytest -v tests/

morning:
	python -m src.morning

intraday:
	python -m src.intraday

eod:
	python -m src.eod

# Quick dry-run that pulls live data but does NOT send Telegram or commit state.
morning-dry:
	DRY_RUN=1 python -m src.morning

intraday-dry:
	DRY_RUN=1 python -m src.intraday

eod-dry:
	DRY_RUN=1 python -m src.eod

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache
