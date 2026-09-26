PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: dev run app test lint icons clean

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet --upgrade pip

dev: $(BIN)/python
	$(BIN)/pip install --quiet -e ".[app,dev]"

run: dev
	$(BIN)/python -m crate_relay

app:
	./scripts/build.sh

test: dev
	$(BIN)/python -m pytest -q

lint: dev
	$(BIN)/ruff check src tests scripts

icons: dev
	$(BIN)/python scripts/make_icons.py

clean:
	rm -rf build dist *.egg-info src/*.egg-info
