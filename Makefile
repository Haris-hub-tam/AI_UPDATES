# AI updates portal. Type `make` to list the commands.

PORT ?= 8801
HOST ?= 0.0.0.0
UV   ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)
PID  := .portal.pid
LOG  := portal.log

.DEFAULT_GOAL := help
.PHONY: help install run start stop restart status logs refresh translate test

help:  ## List the commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-9s %s\n", $$1, $$2}'
	@echo "  Port: $(PORT) (change with: make start PORT=9000)"

install:  ## Install or update the dependencies
	$(UV) sync

run: install  ## Start the portal in this terminal (Ctrl+C to stop)
	$(UV) run uvicorn app:app --host $(HOST) --port $(PORT)

start: install  ## Start the portal in the background (log in portal.log)
	@if [ -f $(PID) ] && kill -0 $$(cat $(PID)) 2>/dev/null; then \
		echo "Already running (pid $$(cat $(PID))) on port $(PORT). After a code change, use: make restart"; exit 0; fi; \
	if ss -ltn 2>/dev/null | grep -q ":$(PORT) "; then \
		echo "Port $(PORT) is already used by another program. Stop it first, or use PORT=..."; exit 1; fi; \
	nohup $(UV) run uvicorn app:app --host $(HOST) --port $(PORT) >> $(LOG) 2>&1 & echo $$! > $(PID); \
	for i in $$(seq 1 30); do \
		if curl -fsS -o /dev/null http://127.0.0.1:$(PORT)/api/status 2>/dev/null; then \
			echo "Portal running: http://$$(hostname -I | awk '{print $$1}'):$(PORT)  (pid $$(cat $(PID)))"; exit 0; fi; \
		sleep 1; done; \
	echo "The portal did not answer within 30 s. See: make logs"; exit 1

stop:  ## Stop the background portal
	@if [ -f $(PID) ] && kill -0 $$(cat $(PID)) 2>/dev/null; then \
		kill $$(cat $(PID)) && echo "Stopped (pid $$(cat $(PID)))."; \
		for i in $$(seq 1 20); do ss -ltn 2>/dev/null | grep -q ":$(PORT) " || break; sleep 0.5; done; \
	else echo "Not running."; fi; \
	rm -f $(PID)

restart: stop start  ## Stop, then start again (after a code change)

status:  ## Show whether the portal is running
	@if [ -f $(PID) ] && kill -0 $$(cat $(PID)) 2>/dev/null; then \
		echo "Running (pid $$(cat $(PID))): http://$$(hostname -I | awk '{print $$1}'):$(PORT)"; \
	else echo "Not running."; fi

logs:  ## Follow the portal log (Ctrl+C to quit)
	@touch $(LOG); tail -n 50 -f $(LOG)

refresh:  ## Collect news, prices and benchmarks now (same as the button)
	$(UV) run collector.py

translate:  ## Translate all titles, teasers and weekly summaries not translated yet (uses the AI provider)
	$(UV) run collector.py --translate

test:  ## Run the tests
	$(UV) run pytest -q
