PYTHON ?= python3

.PHONY: install run

install:
	$(PYTHON) -m venv .venv
	. .venv/bin/activate && pip install -r requirements.txt

run:
	AGENT_NAME=sales-girl-agent-fr python main.py dev & \
	AGENT_NAME=sales-girl-agent-en python main.py dev

