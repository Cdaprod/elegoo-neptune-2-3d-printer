SHELL := /bin/sh
.DEFAULT_GOAL := help
.PHONY: help setup up down restart ps logs check token rotate-token

PYTHON ?= python3

help:
	@printf '%s\n' 'make setup PRINTER_IP=192.168.0.50  # securely prompt for existing OctoPrint API key' 'make up                              # validate and launch both containers' 'make check                           # validate root Compose configuration' 'make token                           # show existing Observatory token locally' 'make rotate-token                    # generate and save a new Observatory token' 'make logs                            # follow both services'

setup:
	@$(PYTHON) scripts/setup_env.py --printer-ip "$(PRINTER_IP)"

up: check
	@docker compose up -d --build

check:
	@docker compose config --quiet

down:
	@docker compose down

restart:
	@docker compose restart

ps:
	@docker compose ps

logs:
	@docker compose logs -f --tail=100 octoprint neptune-observatory

token:
	@$(PYTHON) scripts/setup_env.py --show-token

rotate-token:
	@$(PYTHON) scripts/setup_env.py --rotate-token
