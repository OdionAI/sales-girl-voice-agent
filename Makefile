PYTHON ?= python3

.PHONY: install run

install:
	$(PYTHON) -m venv .venv
	. .venv/bin/activate && pip install -r requirements.txt

run:
	python main.py dev

