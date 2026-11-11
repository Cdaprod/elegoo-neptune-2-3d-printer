SHELL := /bin/sh
.DEFAULT_GOAL := help
.PHONY: help setup authorize-octoprint up down restart ps logs check token rotate-token

PYTHON ?= python3

help:
	@printf '%s\n' 'make setup                           # configure printer and Observatory token' 'make authorize-octoprint             # request browser-approved OctoPrint application key' 'make up                              # reuse valid API key and launch both containers' 'make check                           # validate root Compose configuration' 'make token                           # show existing Observatory token locally' 'make rotate-token                    # generate and save a new Observatory token' 'make logs                            # follow both services'

setup:
	@$(PYTHON) scripts/setup_env.py

authorize-octoprint: setup
	@OCTOPRINT_API_KEY=authorization-pending docker compose stop neptune-observatory
	@OCTOPRINT_API_KEY=authorization-pending docker compose up -d --build octoprint
	@$(PYTHON) scripts/authorize_octoprint.py

up: setup
	@OCTOPRINT_API_KEY=authorization-pending docker compose up -d --build octoprint
	@$(PYTHON) scripts/authorize_octoprint.py
	@$(MAKE) check
	@docker compose up -d --build

check:
	@$(PYTHON) scripts/authorize_octoprint.py --check-config
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
