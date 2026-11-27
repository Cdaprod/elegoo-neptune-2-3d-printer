SHELL := /bin/sh
.DEFAULT_GOAL := help
.PHONY: deploy kiosk help setup authorize-octoprint reauthorize-octoprint up down restart ps logs check token rotate-token sd-rebuild

PYTHON ?= python3

help:
	@printf '%s\n' 'make deploy                          # start services and launch desktop kiosk' 'make kiosk                           # start fullscreen browser in background' 'make setup                           # configure printer and Observatory token' 'make authorize-octoprint             # validate or provision OctoPrint application key' 'make reauthorize-octoprint           # explicitly replace a revoked OctoPrint key' 'make up                              # reuse valid API key and launch both containers' 'make check                           # validate root Compose configuration' 'make token                           # show existing Observatory token locally' 'make rotate-token                    # generate and save a new Observatory token' 'make logs                            # follow both services' 'make sd-rebuild                      # backup and rebuild inserted SD card safely' 'make sd-import                       # dry-run USB SD import, auto-detect one card' 'make sd-import APPLY=1               # import copies to OctoPrint local storage'

setup:
	@$(PYTHON) scripts/setup_env.py

authorize-octoprint: setup usb-check
	@OCTOPRINT_API_KEY=authorization-pending docker compose stop neptune-observatory
	@OCTOPRINT_API_KEY=authorization-pending docker compose up -d --build octoprint
	@$(PYTHON) scripts/authorize_octoprint.py

reauthorize-octoprint: setup usb-check
	@OCTOPRINT_API_KEY=authorization-pending docker compose up -d octoprint
	@$(PYTHON) scripts/authorize_octoprint.py --reauthorize

up: setup usb-check
	@docker volume inspect mks-wifi_commissioning-data >/dev/null 2>&1 || docker volume create mks-wifi_commissioning-data >/dev/null
	@OCTOPRINT_API_KEY=authorization-pending docker compose up -d octoprint
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

sd-rebuild:
	@$(PYTHON) scripts/neptune_sd_rebuild.py

deploy: appliance-config kiosk-setup up
	@bash deploy/raspberry-pi/kiosk/manage.sh enable
	@$(MAKE) kiosk-restart

kiosk:
	@bash deploy/raspberry-pi/kiosk/manage.sh start

kiosk-debug:
	@bash deploy/raspberry-pi/kiosk/launch.sh

.PHONY: kiosk-debug kiosk-stop kiosk-restart kiosk-status kiosk-logs kiosk-enable kiosk-disable
kiosk-stop kiosk-restart kiosk-status kiosk-logs kiosk-enable kiosk-disable:
	@bash deploy/raspberry-pi/kiosk/manage.sh $(@:kiosk-%=%)

.PHONY: kiosk-setup
kiosk-setup:
	@$(PYTHON) -m venv "$$HOME/.local/share/neptune-kiosk/venv"
	@"$$HOME/.local/share/neptune-kiosk/venv/bin/python" -m pip install "playwright==1.56.0"

.PHONY: appliance-config
appliance-config:
	@bash deploy/raspberry-pi/configure-appliance.sh

.PHONY: sd-import
sd-import:
	@$(PYTHON) scripts/import_sd_gcode.py $(if $(SD_MOUNT),"$(SD_MOUNT)",) $(if $(filter 1,$(APPLY)),--apply,)

.PHONY: usb-check
usb-check: setup
	@$(PYTHON) scripts/check_usb_owner.py
